#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Provision the AgentCore Runtime for admin-chat-agent (BI assistant).

terraform 이 만들지 않는 리소스를 여기서 보장한다 — web_search Gateway 와
같은 패턴(`provision_agentcore_websearch.py`). terraform 은 ECR repo·execution
role·staging bucket·tool Lambdas·secret 셸까지만 만들고, Runtime 등록과 이미지
빌드는 이 스크립트가 담당한다.

Idempotent (re-run safe): 같은 이름의 Runtime 을 재사용한다. 이름을 바꾸면
find_runtime() 이 못 찾아 중복 Runtime 이 생기고 admin-api env 는 옛 ARN 을
가리킨 채로 남는다 — 이름 변경 시 AGENT_NAME 과 주입 env 를 함께 갱신할 것
(websearch 프로비저너의 GW_NAME 규칙과 동일).

Inbound auth 는 AWS_IAM(SigV4) — admin-api pod 이 IRSA 자격증명으로
bedrock-agentcore:InvokeAgentRuntime 을 호출하므로 JWT/Cognito 불필요.

Subcommands:
    deploy    (default) chat_reader DB 비밀번호 보장(SM 생성 → kubectl pod 으로
              ALTER ROLE) → ECR image 확인(없으면 docker/finch build+push 시도) →
              Runtime create/reuse → READY 대기 → AGENTCORE_RUNTIME_ARN 출력.
    status    현재 Runtime 의 arn/status 를 출력.
    teardown  Runtime 삭제 (terraform 인프라는 그대로 남음).

비밀번호 경로: deploy 호스트는 게이트웨이 VPC 밖이라 psql 직접 접속이 안
되지만 클러스터 내부 실행은 된다.
  eks: kubectl 일회성 pod(migration 이미지) — 임시 k8s secret → pod env
       → psql "password" stdin. ALTER 성공 후에만 put-secret-value.
  ecs: aws ecs run-task(chat-reader-pw task def) — task def 의 secrets 필드가
       SM 두 개(master + chat_reader)를 env 로 주입 → 같은 "password" stdin.
       secrets 는 태스크 시작 시 해석되므로 ECS 는 SM 기록이 ALTER 에 선행한다
       — ALTER 실패 시 SM≠DB 불일치가 남지만 다음 apply 재시도가 수렴한다.
어느 쪽이든 argv/SQL 텍스트/실행 spec 에 평문 비밀번호는 남지 않는다.

Env:
    REGION              Runtime 리전 (default ap-northeast-2 — admin-api 기본값과 일치)
    AGENT_NAME          Runtime 이름 (terraform output chat_agent_name; [a-zA-Z][a-zA-Z0-9_]{0,47})
    ECR_REPO            terraform output chat_agent_ecr_url
    IMAGE_TAG           컨테이너 태그 (default latest)
    ROLE_ARN            terraform output chat_agent_execution_role_arn
    LAMBDA_QUERY_DB     query_db Lambda 이름 (db_tools 활성 시)
    LAMBDA_GET_SCHEMA   get_schema Lambda 이름 (db_tools 활성 시)
    CHAT_STAGING_BUCKET staging 버킷 이름 (리포트 저장용)
    AWS_REGION          Runtime env 의 AWS_REGION (default = REGION)

DB 비밀번호 단계 (chat_reader):
    CHAT_READER_SECRET_ARN  terraform output chat_agent_reader_secret_arn
    ─ eks 경로 (ECS_* 미설정 시) ─
    K8S_NAMESPACE     게이트웨이 네임스페이스 (default llm-gateway)
    KUBE_CONTEXT      kubectl context (미지정 시 current-context)
    MIGRATION_IMAGE   psql 이 포함된 이미지 URI (llm-gateway/migration:<tag>)
    DB_HOST           Aurora cluster endpoint (writer — ALTER ROLE 필요)
    DB_NAME           default gateway
    DB_MASTER_USER    default postgres_admin
    DB_MASTER_SECRET  master 자격증명 k8s secret (default llm-gateway-db)
    DB_MASTER_KEY     그 안의 password 키 (default master_password)
    ─ ecs 경로 (ECS_PW_TASKDEF_ARN 설정 시 자동 선택) ─
    ECS_CLUSTER       클러스터 이름/ARN (output ecs_cluster_name)
    ECS_PW_TASKDEF_ARN  chat-reader-pw task def ARN
    ECS_SUBNETS       run-task 서브넷 — 쉼표 목록 (output private_subnet_ids)
    ECS_SG            task SG id (output task_security_group_id)
    ECS_REGION        run-task 리전 (미지정 시 secret ARN 에서 추출)
    CHAT_READER_SECRET_ARN/ECS_PW_TASKDEF_ARN 둘 다 비어 있으면 이 단계는 건너뛴다.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import time

import boto3
from botocore.exceptions import ClientError

REGION = os.environ.get("REGION", "ap-northeast-2")
# ⚠️ AGENT_NAME 은 Runtime 이름 완전일치로 찾는다 — 이름이 다르면 기존 것을
#    재사용하지 않고 중복 Runtime 을 만든다 (GW_NAME 규칙과 동일).
AGENT_NAME = os.environ.get("AGENT_NAME", "llm_gateway_dev_chat_agent")
ECR_REPO = os.environ.get("ECR_REPO", "")
IMAGE_TAG = os.environ.get("IMAGE_TAG", "latest")
ROLE_ARN = os.environ.get("ROLE_ARN", "")
AGENT_DIR = os.environ.get("AGENT_DIR", "admin-chat-agent")

NAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{0,47}$")
# 커스텀 Code Interpreter (§49) — execution role 을 주입해야 샌드박스가
# S3(staging/·reports/)에 직접 쓸 수 있다. 이름도 Runtime 과 같은 규칙.
INTERPRETER_NAME = os.environ.get(
    "INTERPRETER_NAME", f"{AGENT_NAME}_ci")[:48]


def log(msg: str) -> None:
    print(f"[provision] {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"[provision] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _control():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


# ── ECR image ─────────────────────────────────────────────────────────────────
def image_uri() -> str:
    return f"{ECR_REPO}:{IMAGE_TAG}"


def image_exists() -> bool:
    repo_name = ECR_REPO.split("/", 1)[-1] if "/" in ECR_REPO else ECR_REPO
    ecr = boto3.client("ecr", region_name=_ecr_region())
    try:
        ecr.describe_images(repositoryName=repo_name,
                            imageIds=[{"imageTag": IMAGE_TAG}])
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] in ("ImageNotFoundException",
                                           "RepositoryNotFoundException"):
            return False
        raise


def _ecr_region() -> str:
    # {acct}.dkr.ecr.{region}.amazonaws.com 형식에서 리전 추출 — ECR 은 보통
    # 배포 리전(ap-south-1)이고 Runtime 리전(ap-northeast-2)과 다르다.
    m = re.search(r"\.dkr\.ecr\.([a-z0-9-]+)\.amazonaws\.com", ECR_REPO)
    return m.group(1) if m else REGION


def ensure_image() -> None:
    if not ECR_REPO:
        fail("ECR_REPO 미지정 — terraform output chat_agent_ecr_url 을 넘기세요")
    if image_exists():
        log(f"ECR image 재사용: {image_uri()}")
        return
    # 이미지가 없으면 로컬 빌드 시도 — AgentCore 는 linux/arm64 필수.
    builder = next((b for b in ("finch", "docker")
                    if subprocess.run(["which", b], capture_output=True).returncode == 0), None)
    if not builder:
        fail(f"ECR 에 {image_uri()} 가 없고 docker/finch 도 없습니다 — "
             f"수동으로 빌드하세요:\n"
             f"  docker build --platform linux/arm64 -t {image_uri()} {AGENT_DIR}/\n"
             f"  docker push {image_uri()}")
    acct = ECR_REPO.split(".", 1)[0]
    ecr_reg = f"{acct}.dkr.ecr.{_ecr_region()}.amazonaws.com"
    log(f"ECR image 없음 — {builder} 로 linux/arm64 빌드·푸시합니다: {image_uri()}")
    token = boto3.client("ecr", region_name=_ecr_region()).get_authorization_token()
    pw = base64.b64decode(
        token["authorizationData"][0]["authorizationToken"]).decode().split(":", 1)[1]
    p = subprocess.run([builder, "login", "--username", "AWS", "--password-stdin", ecr_reg],
                       input=pw, text=True)
    if p.returncode != 0:
        fail("ECR login 실패")
    r = subprocess.run(
        [builder, "build", "--platform", "linux/arm64", "-t", image_uri(), AGENT_DIR + "/"])
    if r.returncode != 0:
        fail(f"{builder} build 실패 — 수동 빌드 후 재시도하세요")
    r = subprocess.run([builder, "push", image_uri()])
    if r.returncode != 0:
        fail(f"{builder} push 실패")


# ── chat_reader DB 비밀번호 ────────────────────────────────────────────────────
def _kubectl(*argv: str, stdin: str | None = None) -> subprocess.CompletedProcess:
    ns = os.environ.get("K8S_NAMESPACE", "llm-gateway")
    cmd = ["kubectl", "-n", ns]
    if os.environ.get("KUBE_CONTEXT"):
        cmd += ["--context", os.environ["KUBE_CONTEXT"]]
    cmd += list(argv)
    return subprocess.run(cmd, input=stdin, text=True, capture_output=True)


def ensure_db_password(dry: bool) -> None:
    """gateway_chat_reader 비밀번호를 보장한다 — deploy 호스트는 게이트웨이
    VPC 밖이라 psql 이 직접 못 닿으므로 클러스터 안 실행 경로를 쓴다.
    eks: kubectl 일회성 pod(migration 이미지). ecs: aws ecs run-task
    (chat-reader-pw task def). ECS_PW_TASKDEF_ARN 이 있으면 ecs 경로."""
    sm_arn = os.environ.get("CHAT_READER_SECRET_ARN", "")
    if not sm_arn:
        log("CHAT_READER_SECRET_ARN 미지정 — chat_reader 비밀번호 단계 건너뜀")
        return
    sm_region = sm_arn.split(":")[3]
    sm = boto3.client("secretsmanager", region_name=sm_region)
    raw = None
    try:
        raw = sm.get_secret_value(SecretId=sm_arn).get("SecretString")
    except ClientError:
        pass
    pw = None
    write = False
    if raw:
        try:
            # query_db / seed_dev_data 는 {"username","password"} JSON 계약.
            pw = json.loads(raw)["password"]
            log("chat_reader 비밀번호 — SM 기존 값 재사용")
        except ValueError:
            # 레거시 평문(구 프로비저너/수동 생성) — 비밀번호는 재사용하되
            # JSON 형식으로 재기록해 Lambda 계약에 수렴한다.
            pw = raw
            write = True
            log("chat_reader 비밀번호 — 평문 형식 감지, JSON 형식으로 재기록")
        except (KeyError, TypeError):
            # JSON 이지만 password 키가 없음 — 사용 불가 값, 재생성으로 수렴.
            log("⚠ SM 값이 JSON 이지만 password 키 없음 — 새 비밀번호로 재생성")
    if not pw:
        pw = secrets.token_urlsafe(24)  # alnum+-_ — psql \\password 가 처리
        write = True

    if os.environ.get("ECS_PW_TASKDEF_ARN"):
        _alter_via_ecs(sm, sm_arn, pw, write, dry)
    else:
        _alter_via_kubectl(sm, sm_arn, pw, write, dry)


def _alter_via_ecs(sm, sm_arn: str, pw: str, write: bool, dry: bool) -> None:
    cluster = os.environ.get("ECS_CLUSTER", "")
    taskdef = os.environ["ECS_PW_TASKDEF_ARN"]
    subnets = [s.strip() for s in
               os.environ.get("ECS_SUBNETS", "").split(",") if s.strip()]
    sg = os.environ.get("ECS_SG", "")
    region = os.environ.get("ECS_REGION") or sm_arn.split(":")[3]
    if not (cluster and subnets and sg):
        log("⚠ ECS_CLUSTER/ECS_SUBNETS/ECS_SG 부족 — 비밀번호 동기화 건너뜀")
        return
    if dry:
        log(f"[dry-run] ecs run-task {taskdef} 로 ALTER ROLE gateway_chat_reader"
            f"{' + SM 기록' if write else ''}")
        return
    # task def 의 secrets 주입은 태스크 시작 시 SM 을 해석한다 — SM 기록이
    # ALTER 실행보다 선행돼야 컨테이너가 새 값을 받는다 (eks 경로와 순서가 다름).
    if write:
        sm.put_secret_value(SecretId=sm_arn, SecretString=_secret_json(pw))
        log("chat_reader 비밀번호 기록 → Secrets Manager")
    ecs = boto3.client("ecs", region_name=region)
    r = ecs.run_task(
        cluster=cluster, taskDefinition=taskdef, launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": subnets, "securityGroups": [sg],
            "assignPublicIp": "DISABLED"}})
    if r.get("failures") or not r.get("tasks"):
        why = (r["failures"][0].get("reason") if r.get("failures")
               else "tasks 가 비어 있음")
        log(f"⚠ run-task 실패 — 다음 apply 재시도로 수렴합니다: {why}")
        return
    task_arn = r["tasks"][0]["taskArn"]
    log(f"chat_reader 비밀번호 task 실행: {task_arn.rsplit('/', 1)[-1]}")
    ecs.get_waiter("tasks_stopped").wait(
        cluster=cluster, tasks=[task_arn],
        WaiterConfig={"Delay": 5, "MaxAttempts": 60})
    desc = ecs.describe_tasks(cluster=cluster, tasks=[task_arn])
    task = desc["tasks"][0] if desc.get("tasks") else {}
    code = (task.get("containers") or [{}])[0].get("exitCode")
    if code == 0:
        log("ALTER ROLE gateway_chat_reader 완료 — 비밀번호 동기화됨")
    else:
        reason = task.get("stoppedReason", "?")
        log_group = os.environ.get("ECS_PW_LOG_GROUP", "")
        log(f"⚠ ALTER ROLE task 실패(exit={code}, {reason}) — SM≠DB 불일치가 "
            "다음 apply 재시도로 수렴합니다."
            + (f" 로그: aws logs tail {log_group} --region {region}"
               if log_group else ""))


def _secret_json(pw: str) -> str:
    """query_db·seed_dev_data 가 json.loads(SecretString) 하는 계약 형식."""
    return json.dumps({"username": "gateway_chat_reader", "password": pw})


def _alter_via_kubectl(sm, sm_arn: str, pw: str, write: bool, dry: bool) -> None:
    """비밀번호 흐름: provisioner 가 생성 → Secrets Manager (query_db Lambda 가 읽음)
    → 임시 k8s secret → pod env → psql \\password stdin. argv·SQL 텍스트·pod spec
    어디에도 평문이 남지 않는다 (\\password 는 새 비밀번호를 stdin 프롬프트로 받음).
    """
    if not os.environ.get("MIGRATION_IMAGE") or not os.environ.get("DB_HOST"):
        log("⚠ MIGRATION_IMAGE/DB_HOST 미지정 — chat_reader 비밀번호 단계 "
            "건너뜀 (query_db Lambda 가 이전 비밀번호를 계속 씁니다)")
        return
    if subprocess.run(["which", "kubectl"],
                      capture_output=True).returncode != 0:
        log("⚠ kubectl 없음 — chat_reader 비밀번호 동기화 건너뜀 "
            "(SM 값과 DB 가 불일치하면 query_db 가 실패합니다)")
        return
    if dry:
        log(f"[dry-run] SM secret {sm_arn} 에 비밀번호{' 기록' if write else ''}"
            " + kubectl pod 으로 ALTER ROLE gateway_chat_reader")
        return

    tmp_secret = f"chat-reader-pw-{secrets.token_hex(4)}"
    pod = f"chat-reader-pw-{secrets.token_hex(4)}"
    manifest = f"""apiVersion: v1
kind: Secret
metadata:
  name: {tmp_secret}
type: Opaque
data:
  password: {base64.b64encode(pw.encode()).decode()}
"""
    p = _kubectl("apply", "-f", "-", stdin=manifest)
    if p.returncode != 0:
        # 부분 생성됐을 가능성에 대비해 임시 secret 정리 — 비밀번호를 담은
        # 고아 리소스를 네임스페이스에 남기지 않는다.
        _kubectl("delete", "secret", tmp_secret, "--ignore-not-found",
                 "--wait=false")
        log(f"⚠ 임시 secret 생성 실패 — 비밀번호 동기화 건너뜀: "
            f"{p.stderr.strip()[:200]}")
        return
    # kubectl run --overrides 는 strategic merge 라 containers 가 name 키로
    # 합쳐진다 — run 이 만드는 컨테이너 이름은 pod 이름이라 "psql" 로 두면
    # 기본 CMD 컨테이너 + psql 컨테이너가 같이 떠 pod 가 Succeeded 되지
    # 못한다. 명시적 Pod manifest 로 create 해 merge 의존을 제거한다.
    pod_manifest = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod},
        "spec": {
            "restartPolicy": "Never",
            "containers": [{
                "name": "psql",
                "image": os.environ["MIGRATION_IMAGE"],
                "env": [
                    {"name": "PGHOST", "value": os.environ["DB_HOST"]},
                    {"name": "PGPORT", "value": os.environ.get("DB_PORT", "5432")},
                    {"name": "PGUSER", "value": os.environ.get("DB_MASTER_USER", "postgres_admin")},
                    {"name": "PGDATABASE", "value": os.environ.get("DB_NAME", "gateway")},
                    {"name": "PGSSLMODE", "value": "require"},
                    {"name": "PGPASSWORD", "valueFrom": {"secretKeyRef": {
                        "name": os.environ.get("DB_MASTER_SECRET", "llm-gateway-db"),
                        "key": os.environ.get("DB_MASTER_KEY", "master_password")}}},
                    {"name": "CHAT_PW", "valueFrom": {"secretKeyRef": {
                        "name": tmp_secret, "key": "password"}}},
                ],
                # psql \password 는 새 비밀번호를 stdin 에서 두 번 읽는다 —
                # argv 와 SQL 텍스트에 비밀번호가 전혀 나타나지 않는다.
                "command": ["sh", "-c",
                            'printf "%s\\n%s\\n" "$CHAT_PW" "$CHAT_PW" | '
                            'psql -v ON_ERROR_STOP=1 -c "\\password gateway_chat_reader"'],
            }],
        }
    }
    try:
        p = _kubectl("create", "-f", "-", stdin=json.dumps(pod_manifest))
        if p.returncode != 0:
            log(f"⚠ psql pod 생성 실패 — 비밀번호 동기화 건너뜀: "
                f"{p.stderr.strip()[:200]}")
            return
        p = _kubectl("wait", "--for=jsonpath={.status.phase}=Succeeded",
                     f"pod/{pod}", "--timeout=300s")
        if p.returncode != 0:
            logs = _kubectl("logs", pod).stdout.strip()
            log(f"⚠ ALTER ROLE pod 실패 — DB 비밀번호 미변경"
                f"{' (SM 도 기존 값 유지)' if write else ''}: {logs[:300]}")
            return
        # DB 가 먼저 새 비밀번호를 받은 뒤에만 SM 을 갱신한다 — SM 먼저 쓰고
        # ALTER 가 실패하면 query_db Lambda 가 SM 의 새 값으로 실패한다.
        if write:
            sm.put_secret_value(SecretId=sm_arn, SecretString=_secret_json(pw))
            log("chat_reader 비밀번호 기록 → Secrets Manager")
        log("ALTER ROLE gateway_chat_reader 완료 — 비밀번호 동기화됨")
    finally:
        _kubectl("delete", "pod", pod, "--ignore-not-found", "--wait=false")
        _kubectl("delete", "secret", tmp_secret, "--ignore-not-found",
                 "--wait=false")


# ── Runtime ───────────────────────────────────────────────────────────────────
def find_runtime():
    ctl = _control()
    for page in ctl.get_paginator("list_agent_runtimes").paginate():
        for it in page.get("agentRuntimes", []):
            if it.get("agentRuntimeName") == AGENT_NAME:
                return ctl.get_agent_runtime(agentRuntimeId=it["agentRuntimeId"])
    return None


def find_interpreter() -> str:
    """커스텀 Code Interpreter ID — 없거나 조회 실패 시 "".
    죽은(FAILED/삭제중) 항목은 재사용 대상이 아니라 건너뛴다."""
    ctl = _control()
    for page in ctl.get_paginator("list_code_interpreters").paginate():
        for it in page.get("codeInterpreterSummaries", []):
            if it.get("name") != INTERPRETER_NAME:
                continue
            if it.get("status") in ("CREATE_FAILED", "DELETING",
                                   "DELETED", "FAILED"):
                continue
            return it["codeInterpreterId"]
    return ""


def ensure_interpreter(dry: bool) -> str:
    """커스텀 Code Interpreter 생성/재사용 → ID 반환. 실패 시 "" (fail-soft —
    interpreter 없으면 에이전트가 기본 interpreter 로 폴백해 S3 리포트만
    꺼지고 나머지 파이프라인은 산다)."""
    try:
        iid = find_interpreter()
        if iid:
            log(f"Code Interpreter 재사용: {iid}")
            return iid
        # 같은 이름의 죽은(FAILED/삭제중) interpreter 가 있으면 이름 충돌로
        # 생성이 막힌다 — 정리 후 생성한다. 목록의 나머지 동명 항목이 대상.
        for page in _control().get_paginator("list_code_interpreters").paginate():
            for it in page.get("codeInterpreterSummaries", []):
                if it.get("name") != INTERPRETER_NAME:
                    continue
                if it.get("status") in ("CREATE_FAILED", "DELETING",
                                       "DELETED", "FAILED"):
                    log(f"죽은 Code Interpreter({it.get('status')}) 삭제: "
                        f"{it['codeInterpreterId']}")
                    if not dry:
                        _control().delete_code_interpreter(
                            codeInterpreterId=it["codeInterpreterId"])
        if dry:
            log(f"[dry-run] Code Interpreter {INTERPRETER_NAME} 생성")
            return "<dry-interpreter-id>"
        if not ROLE_ARN:
            fail("ROLE_ARN 미지정 — terraform output "
                 "chat_agent_execution_role_arn 을 넘기세요")
        r = _control().create_code_interpreter(
            name=INTERPRETER_NAME,
            executionRoleArn=ROLE_ARN,
            # Runtime 과 같은 이유로 PUBLIC — 샌드박스는 S3/Bedrock 공개 API 만
            # 부른다. VPC 부착은 필요 없고 오히려 엔드포인트 비용만 든다.
            networkConfiguration={"networkMode": "PUBLIC"},
            description="admin-chat-agent report sandbox (NDS auto-provisioned)",
        )
        iid = r["codeInterpreterId"]
        log(f"Code Interpreter created: {iid}")
        return iid
    except ClientError as e:
        log(f"Code Interpreter 생성 실패({e.response['Error']['Code']}) — "
            "S3 리포트 업로드는 비활성 상태로 진행합니다")
        return ""


def _env_vars(interp_id: str = "") -> dict:
    env = {"AWS_REGION": os.environ.get("AWS_REGION", REGION)}
    for k in ("LAMBDA_QUERY_DB", "LAMBDA_GET_SCHEMA", "CHAT_STAGING_BUCKET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    if interp_id:
        env["CODE_INTERPRETER_ID"] = interp_id
    return env


def _env_drift(ex: dict, want_env: dict) -> bool:
    """Runtime env 가 원하는 값과 어긋나는지 — interpreter 추가 같은 env-only
    변경도 update 경로를 타게 하려고 이미지 비교와 별도로 본다."""
    cur = ex.get("environmentVariables") or {}
    return any(cur.get(k) != v for k, v in want_env.items())


def _runtime_image_uri(ex: dict) -> str:
    return ((ex.get("agentRuntimeArtifact") or {})
            .get("containerConfiguration") or {}).get("containerUri", "")


def _wait_ready(rid: str) -> None:
    for _ in range(60):
        st = _control().get_agent_runtime(agentRuntimeId=rid).get("status")
        if st == "READY":
            return
        if st in ("CREATE_FAILED", "UPDATE_FAILED", "DELETING"):
            fail(f"Runtime 상태 이상: {st}")
        time.sleep(5)
    fail("Runtime READY 대기 타임아웃")


def ensure_runtime(dry: bool) -> str:
    # interpreter 는 Runtime env(CODE_INTERPRETER_ID)에 들어가므로 먼저 보장.
    # 실패해도 "" 로 진행 — S3 리포트만 꺼진 채 나머지는 동작한다.
    interp_id = ensure_interpreter(dry)
    want_env = _env_vars(interp_id)
    ex = find_runtime()
    if ex and ex.get("status") in ("CREATE_FAILED", "UPDATE_FAILED",
                                 "DELETING", "DELETED"):
        # 죽은 Runtime 을 이름으로 재사용하면 admin-api 가 영구히 죽은 ARN 을
        # 받는다 — 삭제가 전파될 때까지 기다린 뒤 새로 만든다.
        rid = ex["agentRuntimeId"]
        log(f"Runtime {ex.get('status')} 상태 — 재생성을 위해 삭제: {rid}")
        if not dry:
            if ex.get("status") != "DELETING":
                _control().delete_agent_runtime(agentRuntimeId=rid)
            for _ in range(36):
                if find_runtime() is None:
                    break
                time.sleep(5)
            else:
                fail("Runtime 삭제 전파 타임아웃 — 콘솔에서 삭제 후 재시도하세요")
        ex = None
    if ex:
        arn = ex["agentRuntimeArn"]
        want = image_uri() if ECR_REPO else ""
        current = _runtime_image_uri(ex)
        env_drift = _env_drift(ex, want_env)
        if (want and current != want) or env_drift:
            # 에이전트 코드/태그 변경 — Runtime 은 생성 시점 이미지에 고정되므로
            # 새 태그로 수렴한다. (재사용만 하면 코드 변경이 영영 안 올라감.)
            # env-only drift(CODE_INTERPRETER_ID 신규 등)도 같은 경로로 반영.
            why = f"{current} → {want}" if want and current != want \
                else "env drift"
            if dry:
                log(f"[dry-run] Runtime update: {why}")
                return arn
            ensure_image()
            rid = ex["agentRuntimeId"]
            _control().update_agent_runtime(
                agentRuntimeId=rid,
                agentRuntimeArtifact={
                    "containerConfiguration": {"containerUri": want or current}},
                roleArn=ROLE_ARN or ex.get("roleArn", ""),
                networkConfiguration=ex.get("networkConfiguration")
                    or {"networkMode": "PUBLIC"},
                environmentVariables=want_env,
                description="admin-chat-agent BI assistant (NDS auto-provisioned)",
            )
            log(f"Runtime update 요청: {arn} ({why})")
            _wait_ready(rid)
            log("Runtime READY (updated)")
            return arn
        log(f"Runtime 재사용: {arn} ({ex.get('status')})")
        return arn
    if dry:
        log(f"[dry-run] Runtime {AGENT_NAME} (containerUri={image_uri()}, PUBLIC)")
        return "<dry-runtime-arn>"
    if not NAME_RE.match(AGENT_NAME):
        fail(f"AGENT_NAME '{AGENT_NAME}' 이 이름 규칙을 위반합니다 — "
             "[a-zA-Z][a-zA-Z0-9_] 최대 48자 (하이픈 불가)")
    if not ROLE_ARN:
        fail("ROLE_ARN 미지정 — terraform output chat_agent_execution_role_arn 을 넘기세요")
    ensure_image()
    r = _control().create_agent_runtime(
        agentRuntimeName=AGENT_NAME,
        agentRuntimeArtifact={
            "containerConfiguration": {"containerUri": image_uri()}},
        roleArn=ROLE_ARN,
        # agent 는 Lambda invoke·Bedrock·S3 등 공개 AWS API 만 부른다 —
        # DB 접속은 query_db Lambda(VPC 부착)가 대신하므로 Runtime 자체는 PUBLIC.
        networkConfiguration={"networkMode": "PUBLIC"},
        environmentVariables=want_env,
        description="admin-chat-agent BI assistant (NDS auto-provisioned)",
    )
    arn = r["agentRuntimeArn"]
    rid = r["agentRuntimeId"]
    log(f"Runtime created: {arn}")
    # READY 대기 — CREATING 상태로 admin-api 가 호출하면 실패한다.
    _wait_ready(rid)
    log("Runtime READY")
    return arn


def teardown() -> None:
    # 프로비저너 소유 리소스는 Runtime + Code Interpreter 둘 다 정리한다 —
    # interpreter 는 terraform 밖이라 방치하면 고아 리소스가 된다.
    try:
        iid = find_interpreter()
        if iid:
            _control().delete_code_interpreter(codeInterpreterId=iid)
            log(f"Code Interpreter 삭제 요청: {iid}")
    except ClientError as e:
        log(f"Code Interpreter 삭제 실패({e.response['Error']['Code']}) — "
            "Runtime 삭제는 계속합니다")
    ex = find_runtime()
    if not ex:
        log("Runtime 없음 — 건너뜀")
        return
    _control().delete_agent_runtime(agentRuntimeId=ex["agentRuntimeId"])
    log(f"Runtime 삭제 요청: {ex['agentRuntimeArn']}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", nargs="?", default="deploy",
                    choices=["deploy", "status", "teardown"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.cmd == "status":
        ex = find_runtime()
        if not ex:
            print("  (none)")
            return
        print(f"  name: {ex.get('agentRuntimeName')}")
        print(f"  arn: {ex.get('agentRuntimeArn')}")
        print(f"  status: {ex.get('status')}")
        return
    if args.cmd == "teardown":
        teardown()
        return
    ensure_db_password(args.dry_run)
    arn = ensure_runtime(args.dry_run)
    print(f"AGENTCORE_RUNTIME_ARN={arn}")


if __name__ == "__main__":
    main()
