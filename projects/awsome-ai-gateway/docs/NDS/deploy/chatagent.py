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
AGENTCORE_REGION = os.environ.get("CHAT_AGENT_REGION", "ap-northeast-2")


def _agent_name(outs: dict, env_name: str = "") -> str:
    """Runtime 이름 — outputs 우선, 없으면 도출.

    -target apply 는 관련 없는 output 을 state 에 안 쓴다 — 첫 활성화 때
    chat_agent_name 이 비어 있을 수 있어 {project}-{env}-admin-chat-agent
    (render 가 project=llm-gateway 고정)으로 도출한다. 없으면 provisioner
    기본값(llm_gateway_dev_chat_agent)이 엉뚱한 이름의 Runtime 을 만든다."""
    raw = outs.get("chat_agent_name") or f"llm-gateway-{env_name}-admin-chat-agent"
    return raw.replace("-", "_")


def _env(outs: dict, meta: dict, migration_image: str,
         context: str = "", extras: dict | None = None,
         env_name: str = "") -> dict[str, str]:
    """terraform output → 프로비저너 env. Runtime 이름은 이름 패턴
    ([a-zA-Z][a-zA-Z0-9_]{0,47}) 때문에 하이픈 → 밑줄 정규화가 필요하다."""
    e = dict(os.environ)
    e["REGION"] = AGENTCORE_REGION
    e["AGENT_NAME"] = _agent_name(outs, env_name)
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
    for k, v in (extras or {}).items():
        if v:
            e[k] = str(v)
    return e


def _run(repo_root: Path, sub: str, outs: dict, meta: dict | None = None,
         migration_image: str = "", context: str = "",
         extras: dict | None = None, env_name: str = "",
         timeout: int = 900) -> subprocess.CompletedProcess:
    script = repo_root / PROVISIONER
    if not script.exists():
        return subprocess.CompletedProcess([], 1, "", f"{script} 없음")
    try:
        return subprocess.run(
            [sys.executable, str(script), sub],
            env=_env(outs, meta or {}, migration_image, context, extras, env_name),
            cwd=repo_root, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return subprocess.CompletedProcess([], 1, "", str(exc))


def status_arn(repo_root: Path, outs: dict, env_name: str = "") -> str:
    """기존 Runtime ARN — 없거나 조회 실패 시 "".

    읽기 전용 — plan/preview 경로에서도 안전하다."""
    r = _run(repo_root, "status", outs, env_name=env_name, timeout=120)
    if r.returncode != 0:
        return ""
    m = re.search(r"^  arn: (\S+)", r.stdout, re.M)
    return m.group(1) if m else ""


def ensure_runtime_arn(repo_root: Path, outs: dict, meta: dict | None = None,
                       migration_image: str = "", context: str = "",
                       extras: dict | None = None, env_name: str = "") -> str:
    """deploy(멱등) 실행 — chat_reader 비밀번호 동기화 + Runtime 생성/재사용
    → ARN 반환. 실패 시 "".

    status 조기반환 없이 매번 deploy 를 돌린다 — 비밀번호 ALTER ROLE 도 같이
    재실행돼 수동 변경 드리프트가 apply 마다 자가치유된다. deploy 는 기존
    Runtime 을 재사용하므로 추가 비용은 SM/kubectl·ecs 왕복뿐이다.

    extras: ecs 경로 입력(ECS_CLUSTER/ECS_PW_TASKDEF_ARN/ECS_SUBNETS/ECS_SG/
    ECS_REGION) — eks 경로는 넘기지 않는다."""
    arn = status_arn(repo_root, outs, env_name=env_name)
    if not arn:
        print(f"bi_insight: AgentCore Runtime 이 없어 프로비저닝합니다 "
              f"(region={AGENTCORE_REGION}, name={_agent_name(outs, env_name)})",
              file=sys.stderr)
    r = _run(repo_root, "deploy", outs, meta=meta,
             migration_image=migration_image, context=context, extras=extras,
             env_name=env_name)
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


def teardown(repo_root: Path, env_name: str) -> bool:
    """bi_insight 선언이 꺼졌을 때 AgentCore Runtime 을 지운다 — 멱등.

    terraform 밖 리소스라 destroy 가 못 지운다 — 선언이 진실이려면 apply/\
    teardown 경로에서 여기서 정리한다 (websearch.teardown 과 같은 역할).
    이름은 outputs 없이 도출한다 — render 가 project=llm-gateway 로 고정해
    {project}-{env}-admin-chat-agent 이고, 모듈이 이미 지워진 뒤에도 부를 수
    있어야 하므로 state 조회를 요구하지 않는다."""
    name = f"llm-gateway-{env_name}-admin-chat-agent".replace("-", "_")
    r = _run(repo_root, "teardown", {}, extras={"AGENT_NAME": name},
             timeout=180)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-2:]
        print(f"⚠ chat-agent Runtime teardown 실패 — {'; '.join(tail) or '?'}",
              file=sys.stderr)
        return False
    for line in (r.stderr or "").strip().splitlines():
        if line.startswith("[provision]"):
            print(f"  {line}", file=sys.stderr)
    return True
