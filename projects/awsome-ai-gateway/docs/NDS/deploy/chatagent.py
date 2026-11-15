# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""AgentCore Runtime (admin-chat-agent) 자동 프로비저닝 — websearch.py 와 동일 패턴.

기존 수동 절차(docs/NDS/chat-agent.md):
  1. enable_chat_agent=true tf apply → ECR/role/staging/Lambda
  2. 이미지 build/push → boto3 CreateAgentRuntime → ARN 확보
  3. admin-api env 주입 (AGENTCORE_RUNTIME_ARN 등)

`deploy apply` 경로에서는 features.bi_insight=true 만으로 2~3 을 자동 수행한다
(1 의 enable_chat_agent 는 managed overlay tfvars 로 내려간다). 프로비저너는
멱등(같은 이름의 Runtime 재사용)이라 apply 를 다시 돌려도 안전하다. 실패해도
배포를 중단하지 않는다 — env 미주입으로 라우터가 503 을 반환하는 graceful-off
상태로 두고 경고를 낸다 (websearch 와 같은 의미).

⚠️ AGENT_NAME 규칙: Runtime 은 이름 완전일치로 찾는다 — 이름이 바뀌면 기존
Runtime 을 재사용하지 않고 중복이 생긴다 (websearch 의 GW_NAME 주석 참조).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

PROVISIONER = Path("deployment/scripts/provision_agentcore_chatagent.py")


def resolved_region(region: str = "") -> str:
    """Runtime 리전 해결 — CHAT_AGENT_REGION env > 호출부 배포 리전 > 최후 기본값.

    env 만 보면 export 를 잊은 셸이 조용히 다른 리전(기본 ap-northeast-2)을
    조회해 "Runtime 없음" 오판 → ensure 경로가 빈 ARN 을 내려 rendered 가
    채팅 env 전부를 닦는 사고가 실제로 났었다(배포 리전과 Runtime 리전은 ECR
    제약상 항상 같아야 하므로 cfg.aws.region 이 안전한 fallback 이다)."""
    return os.environ.get("CHAT_AGENT_REGION") or region or "ap-northeast-2"


def _agent_name(outs: dict, env_name: str = "") -> str:
    """Runtime 이름 — outputs 우선, 없으면 도출.

    -target apply 는 관련 없는 output 을 state 에 안 쓴다 — 첫 활성화 때
    chat_agent_name 이 비어 있을 수 있어 {project}-{env}-admin-chat-agent
    (render 가 project=llm-gateway 고정)으로 도출한다. 없으면 provisioner
    기본값(llm_gateway_dev_chat_agent)이 엉뚱한 이름의 Runtime 을 만든다."""
    raw = outs.get("chat_agent_name") or f"llm-gateway-{env_name}-admin-chat-agent"
    return raw.replace("-", "_")


def _git_sha(repo_root: Path | None) -> str:
    """git HEAD short SHA — rebuild-image.sh 와 같은 태그 규약.
    미커밋 변경이 있으면 -dirty 접미 — 같은 SHA 라 image_exists 가 옛
    이미지를 재사용해 미커밋 수정이 영영 안 올라가는 함정을 막는다."""
    if not repo_root:
        return ""
    try:
        r = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                           cwd=repo_root, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return ""
        sha = r.stdout.strip()
        d = subprocess.run(["git", "status", "--porcelain", "--",
                            "admin-chat-agent/"],
                           cwd=repo_root, capture_output=True, text=True, timeout=10)
        if d.returncode == 0 and d.stdout.strip():
            sha += "-dirty"
        return sha
    except (OSError, subprocess.TimeoutExpired):
        return ""


# 프로비저너 입력 키 — 부모 쉘의 stale export 가 outputs/파라미터를 조용히
# 이기는 걸 막는다. 수동 절차 문서가 이 변수들을 export 하라고 안내하기
# 때문에 eks 경로에서도 실제로 박혀 있는 경우가 있다 (예: ECS_PW_TASKDEF_ARN
# 이 있으면 eks 배포가 ecs run-task 로 ALTER 를 시도해 엉뚱한 DB 를 건드린다).
_PROVISIONER_INPUTS = frozenset({
    "AGENT_NAME", "ECR_REPO", "ROLE_ARN", "ROLE_NAME",
    "LAMBDA_QUERY_DB", "LAMBDA_GET_SCHEMA", "CHAT_STAGING_BUCKET",
    "CHAT_READER_SECRET_ARN", "DB_HOST", "DB_NAME", "DB_PORT",
    "DB_MASTER_SECRET", "DB_MASTER_KEY",
    "ECS_CLUSTER", "ECS_PW_TASKDEF_ARN", "ECS_SUBNETS", "ECS_SG",
    "ECS_REGION", "KUBE_CONTEXT", "K8S_NAMESPACE", "MIGRATION_IMAGE",
    "INTERPRETER_NAME", "AGENT_DIR", "AWS_REGION", "REGION",
    "TARGET_NAME", "CALLER_ROLE_ARN", "GW_NAME",
})


def _env(outs: dict, meta: dict, migration_image: str,
         context: str = "", extras: dict | None = None,
         env_name: str = "", repo_root: Path | None = None,
         region: str = "") -> dict[str, str]:
    """terraform output → 프로비저너 env. Runtime 이름은 이름 패턴
    ([a-zA-Z][a-zA-Z0-9_]{0,47}) 때문에 하이픈 → 밑줄 정규화가 필요하다."""
    e = {k: v for k, v in os.environ.items()
         if k not in _PROVISIONER_INPUTS}
    e["REGION"] = resolved_region(region)
    e["AGENT_NAME"] = _agent_name(outs, env_name)
    # 에이전트 이미지 태그 — latest 고정이면 Runtime 이 생성 시점 이미지에
    # 고정돼 코드 변경이 apply 로 영영 재배포되지 않는다. SHA 태그를 기본으로
    # 줘서 코드가 바뀌면 containerUri 도 바뀌게(update 경로가 수렴). 운영자가
    # IMAGE_TAG 를 명시하면 그 값이 우선.
    if "IMAGE_TAG" not in e:
        sha = _git_sha(repo_root)
        if sha:
            e["IMAGE_TAG"] = sha
    if outs.get("chat_agent_ecr_url"):
        e["ECR_REPO"] = outs["chat_agent_ecr_url"]
    if outs.get("chat_agent_execution_role_arn"):
        e["ROLE_ARN"] = outs["chat_agent_execution_role_arn"]
    for k, out_key in (("LAMBDA_QUERY_DB", "chat_agent_query_db_function"),
                       ("LAMBDA_GET_SCHEMA", "chat_agent_get_schema_function"),
                       ("CHAT_STAGING_BUCKET", "chat_agent_staging_bucket"),
                       ("CHAT_READER_SECRET_ARN", "chat_agent_reader_secret_arn"),
                       ("DB_NAME", "aurora_database_name")):
        if outs.get(out_key):
            e[k] = outs[out_key]
    # eks env 는 aurora_endpoint, ecs env 는 db_endpoint 로 출력한다
    db_host = outs.get("aurora_endpoint") or outs.get("db_endpoint")
    if db_host:
        e["DB_HOST"] = db_host
    if meta.get("namespace"):
        e["K8S_NAMESPACE"] = meta["namespace"]
    if context:
        e["KUBE_CONTEXT"] = context
    if migration_image:
        e["MIGRATION_IMAGE"] = migration_image
    # ALTER pod 의 master 자격증명 k8s secret — 차트 값
    # database.external.passwordSecretName/masterPasswordSecretKey 와 맞춰야
    # 한다. 커스텀 채택 배포는 기본값(llm-gateway-db/master_password)이 아니라
    # 값을 선언하므로 레이어를 병합해 읽는다 (_migration_image 와 같은 패턴).
    if meta.get("values_layers") and repo_root:
        import yaml
        db_sec = db_key = ""
        for layer in meta["values_layers"]:
            lp = Path(layer)
            lp = lp if lp.is_absolute() else repo_root / lp
            try:
                v = yaml.safe_load(lp.read_text())
            except (OSError, UnicodeDecodeError, yaml.YAMLError):
                continue
            ext = (v.get("database") or {}).get("external") \
                if isinstance(v, dict) else None
            if not isinstance(ext, dict):
                continue
            db_sec = str(ext.get("passwordSecretName") or db_sec)
            db_key = str(ext.get("masterPasswordSecretKey") or db_key)
        if db_sec:
            e["DB_MASTER_SECRET"] = db_sec
        if db_key:
            e["DB_MASTER_KEY"] = db_key
    for k, v in (extras or {}).items():
        if v:
            e[k] = str(v)
    return e


def _run(repo_root: Path, sub: str, outs: dict, meta: dict | None = None,
         migration_image: str = "", context: str = "",
         extras: dict | None = None, env_name: str = "",
         timeout: int = 900, region: str = "") -> subprocess.CompletedProcess:
    script = repo_root / PROVISIONER
    if not script.exists():
        return subprocess.CompletedProcess([], 1, "", f"{script} 없음")
    try:
        return subprocess.run(
            [sys.executable, str(script), sub],
            env=_env(outs, meta or {}, migration_image, context, extras, env_name,
                     repo_root=repo_root, region=region),
            cwd=repo_root, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


def status_arn(repo_root: Path, outs: dict, env_name: str = "",
               region: str = "") -> str:
    """기존 Runtime ARN — 없거나 조회 실패 시 "".

    읽기 전용 — plan/preview 경로에서도 안전하다."""
    r = _run(repo_root, "status", outs, env_name=env_name, timeout=120,
             region=region)
    if r.returncode != 0:
        return ""
    m = re.search(r"^  arn: (\S+)", r.stdout, re.M)
    return m.group(1) if m else ""


def ensure_runtime_arn(repo_root: Path, outs: dict, meta: dict | None = None,
                       migration_image: str = "", context: str = "",
                       extras: dict | None = None, env_name: str = "",
                       region: str = "") -> str:
    """deploy(멱등) 실행 — chat_reader 비밀번호 동기화 + Runtime 생성/재사용
    → ARN 반환. 실패 시 "".

    status 조기반환 없이 매번 deploy 를 돌린다 — 비밀번호 ALTER ROLE 도 같이
    재실행돼 수동 변경 드리프트가 apply 마다 자가치유된다. deploy 는 기존
    Runtime 을 재사용하므로 추가 비용은 SM/kubectl·ecs 왕복뿐이다.

    extras: ecs 경로 입력(ECS_CLUSTER/ECS_PW_TASKDEF_ARN/ECS_SUBNETS/ECS_SG/
    ECS_REGION) — eks 경로는 넘기지 않는다."""
    arn = status_arn(repo_root, outs, env_name=env_name, region=region)
    if not arn:
        print(f"bi_insight: AgentCore Runtime 이 없어 프로비저닝합니다 "
              f"(region={resolved_region(region)}, name={_agent_name(outs, env_name)})",
              file=sys.stderr)
    r = _run(repo_root, "deploy", outs, meta=meta,
             migration_image=migration_image, context=context, extras=extras,
             env_name=env_name, region=region)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-3:]
        print(f"⚠ chat-agent 프로비저닝 실패 — {'; '.join(tail) or r.stdout[:200]}",
              file=sys.stderr)
        return ""
    m = re.search(r"^AGENTCORE_RUNTIME_ARN=(\S+)", r.stdout, re.M)
    if not m:
        print(f"⚠ 프로비저너 출력에서 ARN 을 찾지 못했습니다: {r.stdout[:200]}",
              file=sys.stderr)
        return ""
    for line in (r.stderr or "").strip().splitlines():
        if line.startswith("[provision]"):
            print(f"  {line}", file=sys.stderr)
    return m.group(1)


def build_tree_exists(repo_root: Path) -> bool:
    """archive_file data source 가 zip 하는 build/ 트리가 있는가 — plan 같은
    읽기 경로는 존재만 필요하고 최신성은 안 따진다 (pip 재설치 수 분 회피)."""
    b = repo_root / "deployment/terraform/modules/agentcore-runtime/build"
    return all((b / n / "lambda_function.py").exists()
               for n in ("query_db", "get_schema"))


def ensure_lambda_build(repo_root: Path) -> bool:
    """admin-chat-agent/lambdas/build-lambdas.sh 실행 — agentcore-runtime
    모듈의 archive_file data source 가 참조하는 build/{query_db,get_schema}
    트리를 만든다.

    terraform plan/apply 모두 data source 를 평가해 디렉토리가 없으면
    'could not archive missing directory' 로 실패한다 — bi_insight 선언이
    있으면 어떤 tf 명령보다 먼저 돌아야 한다. 스크립트는 platform-targeted
    wheel(manylinux2014_x86_64/py3.12)로 vendoring 해 호스트 아키텍처와
    무관하게 Lambda x86_64 와 맞는다."""
    script = repo_root / "admin-chat-agent/lambdas/build-lambdas.sh"
    try:
        r = subprocess.run(["bash", str(script)], cwd=repo_root,
                           capture_output=True, text=True, timeout=600)
    except (subprocess.TimeoutExpired, OSError) as exc:
        print(f"⚠ Lambda 빌드 실패 — {exc}", file=sys.stderr)
        return False
    if r.returncode != 0:
        tail = (r.stderr or r.stdout or "").strip().splitlines()[-3:]
        print(f"⚠ Lambda 빌드 실패 — {'; '.join(tail)}", file=sys.stderr)
        return False
    print("  Lambda 빌드 완료 (query_db + get_schema)", file=sys.stderr)
    return True


def teardown(repo_root: Path, env_name: str, region: str = "",
             chat_agent_name: str = "") -> bool:
    """bi_insight 선언이 꺼졌을 때 AgentCore Runtime 을 지운다 — 멱등.

    terraform 밖 리소스라 destroy 가 못 지운다 — 선언이 진실이려면 apply/\
    teardown 경로에서 여기서 정리한다 (websearch.teardown 과 같은 역할).
    이름은 outputs 없이 도출한다 — render 가 project=llm-gateway 로 고정해
    {project}-{env}-admin-chat-agent 이고, 모듈이 이미 지워진 뒤에도 부를 수
    있어야 하므로 state 조회를 요구하지 않는다. 호출자가 terraform output 의
    실측 이름을 아는 경우(사람이 project 변수를 바꾼 env 등) 그쪽을 우선한다
    — 도출 이름이 어긋나면 조용히 아무것도 못 지우고 리소스가 새어나간다."""
    name = (chat_agent_name
            or f"llm-gateway-{env_name}-admin-chat-agent").replace("-", "_")
    r = _run(repo_root, "teardown", {}, extras={"AGENT_NAME": name},
             timeout=180, region=region)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-2:]
        print(f"⚠ chat-agent Runtime teardown 실패 — {'; '.join(tail) or '?'}",
              file=sys.stderr)
        return False
    for line in (r.stderr or "").strip().splitlines():
        if line.startswith("[provision]"):
            print(f"  {line}", file=sys.stderr)
    return True
