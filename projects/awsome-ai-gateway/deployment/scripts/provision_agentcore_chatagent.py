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
    deploy    (default) ECR image 확인(없으면 docker/finch build+push 시도) →
              Runtime create/reuse → READY 대기 → AGENTCORE_RUNTIME_ARN 출력.
    status    현재 Runtime 의 arn/status 를 출력.
    teardown  Runtime 삭제 (terraform 인프라는 그대로 남음).

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
"""
from __future__ import annotations

import argparse
import base64
import os
import re
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
    pw = base64.b64decode(token["authorizationData"][0]["authorizationToken"]).decode().split(":", 1)[1]
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


# ── Runtime ───────────────────────────────────────────────────────────────────
def find_runtime():
    ctl = _control()
    for page in ctl.get_paginator("list_agent_runtimes").paginate():
        for it in page.get("agentRuntimes", []):
            if it.get("agentRuntimeName") == AGENT_NAME:
                return ctl.get_agent_runtime(agentRuntimeId=it["agentRuntimeId"])
    return None


def _env_vars() -> dict:
    env = {"AWS_REGION": os.environ.get("AWS_REGION", REGION)}
    for k in ("LAMBDA_QUERY_DB", "LAMBDA_GET_SCHEMA", "CHAT_STAGING_BUCKET"):
        if os.environ.get(k):
            env[k] = os.environ[k]
    return env


def ensure_runtime(dry: bool) -> str:
    ex = find_runtime()
    if ex:
        arn = ex["agentRuntimeArn"]
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
        environmentVariables=_env_vars(),
        description="admin-chat-agent BI assistant (NDS auto-provisioned)",
    )
    arn = r["agentRuntimeArn"]
    rid = r["agentRuntimeId"]
    log(f"Runtime created: {arn}")
    # READY 대기 — CREATING 상태로 admin-api 가 호출하면 실패한다.
    for _ in range(60):
        st = _control().get_agent_runtime(agentRuntimeId=rid).get("status")
        if st == "READY":
            log("Runtime READY")
            return arn
        if st in ("CREATE_FAILED", "DELETING"):
            fail(f"Runtime 상태 이상: {st}")
        time.sleep(5)
    fail("Runtime READY 대기 타임아웃")
    return ""


def teardown() -> None:
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
    arn = ensure_runtime(args.dry_run)
    print(f"AGENTCORE_RUNTIME_ARN={arn}")


if __name__ == "__main__":
    main()
