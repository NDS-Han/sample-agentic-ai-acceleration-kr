# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""LiteLLM 단가 조회 Lambda 자동 프로비저닝 — ops/NDS-01 절차 자동화.

기존 수동 절차 (NDS-01-deploy-litellm-pricing-lambda.sh --apply):
  1. role `llm-gateway-<env>-litellm-pricing` (BasicExecutionRole 만 — DB/Secret 없음)
  2. Lambda `llm-gateway-<env>-litellm-pricing` (python3.12, 256MB, 60s, VPC 없음)
  3. admin-api role 에 `LitellmPricingInvoke` 인라인 정책 (lambda:InvokeFunction)
  4. values/taskdef 에 `LITELLM_PRICING_LAMBDA=<함수이름>` 주입

`features.pricing_lambda: true` 면 apply 가 1~3 을 자동 수행하고 4 는 각
backend 배선 경로로 주입한다 (eks=helm --set, ecs=tfvars → task env + 모듈이
task-role invoke 권한 부여).

명명 규칙은 ops 스크립트와 동일 — 기존 수동 배포본을 재사용/정리할 수 있다.
실패해도 배포를 중단하지 않는다 — LITELLM_PRICING_LAMBDA 가 비면 admin-api 의
litellm 소스가 503/graceful-off 로 건너뛴다 (ops 문서와 같은 의미).
"""
from __future__ import annotations

import io
import json
import sys
import zipfile
from pathlib import Path

LAMBDA_SRC = "litellm-pricing-lambda/lambda_function.py"
RUNTIME = "python3.12"
TIMEOUT, MEMORY = 60, 256
BASIC_EXEC = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
INVOKE_POLICY = "LitellmPricingInvoke"

TRUST = json.dumps({
    "Version": "2012-10-17",
    "Statement": [{"Effect": "Allow",
                   "Principal": {"Service": "lambda.amazonaws.com"},
                   "Action": "sts:AssumeRole"}],
})


def fn_name(env: str) -> str:
    return f"llm-gateway-{env}-litellm-pricing"


def _clients(region: str):
    import boto3
    return (boto3.client("lambda", region_name=region),
            boto3.client("iam", region_name=region),
            boto3.client("sts", region_name=region))


def _zip(src: Path) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(src, "lambda_function.py")
    return buf.getvalue()


def status(region: str, env: str, admin_role_name: str = "") -> dict:
    """읽기 전용 상태 — plan/preview 에서도 안전."""
    lam, iam, _ = _clients(region)
    fn, role, invoke = "미배포", "미생성", "미부여"
    try:
        lam.get_function(FunctionName=fn_name(env))
        fn = "Active"
    except Exception:
        pass
    try:
        iam.get_role(RoleName=fn_name(env))
        role = "존재"
    except Exception:
        pass
    if admin_role_name:
        try:
            iam.get_role_policy(RoleName=admin_role_name,
                                PolicyName=INVOKE_POLICY)
            invoke = "부여됨"
        except Exception:
            pass
    return {"function": fn, "role": role, "invoke": invoke}


def ensure(cfg, repo_root: Path, admin_role_arn: str = "",
           attach_invoke: bool = True) -> str:
    """role+함수+invoke 정책 보장 (멱등). 반환 함수 이름, 실패 시 "".

    attach_invoke=False — ecs 는 task-role 권한이 terraform 모듈 소유라
    boto3 인라인 정책을 붙이지 않는다."""
    region = cfg.aws.region
    name = fn_name(cfg.env)
    src = repo_root / LAMBDA_SRC
    if not src.exists():
        print(f"⚠ pricing_lambda 소스 없음: {src}", file=sys.stderr)
        return ""
    try:
        lam, iam, sts = _clients(region)
        acct = cfg.aws.account_id or \
            sts.get_caller_identity()["Account"]
        role_arn = f"arn:aws:iam::{acct}:role/{name}"

        # ① role — BasicExecutionRole 만 (DB/Secret 접근 없음, ops 와 동일)
        try:
            iam.get_role(RoleName=name)
        except iam.exceptions.NoSuchEntityException:
            iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=TRUST,
                Description="llm-gateway LiteLLM pricing catalog fetch proxy")
            iam.attach_role_policy(RoleName=name, PolicyArn=BASIC_EXEC)
            iam.get_waiter("role_exists").wait(RoleName=name)
            print(f"pricing_lambda: role 생성 {name} (전파 대기)", file=sys.stderr)
            import time
            time.sleep(10)   # role→lambda 전파 — ops 스크립트와 같은 10s

        # ② 함수 — 코드는 매번 갱신 배포 (멱등)
        zipb = _zip(src)
        try:
            lam.get_function(FunctionName=name)
            lam.update_function_code(FunctionName=name, ZipFile=zipb)
            lam.get_waiter("function_updated").wait(FunctionName=name)
            lam.update_function_configuration(
                FunctionName=name, Timeout=TIMEOUT, MemorySize=MEMORY)
        except lam.exceptions.ResourceNotFoundException:
            lam.create_function(
                FunctionName=name, Runtime=RUNTIME,
                Handler="lambda_function.lambda_handler",
                Role=role_arn, Code={"ZipFile": zipb},
                Timeout=TIMEOUT, MemorySize=MEMORY,
                Description="LiteLLM model_catalog fetch proxy "
                            "for llm-gateway admin-api")

        # ③ admin-api role 에 invoke 인라인 정책 (eks — ecs 는 모듈 소유)
        if attach_invoke and admin_role_arn:
            admin_name = admin_role_arn.rsplit("/", 1)[-1]
            iam.put_role_policy(
                RoleName=admin_name, PolicyName=INVOKE_POLICY,
                PolicyDocument=json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Sid": "InvokeLitellmPricing", "Effect": "Allow",
                        "Action": "lambda:InvokeFunction",
                        "Resource": f"arn:aws:lambda:{region}:{acct}"
                                    f":function:{name}"}],
                }))
        print(f"pricing_lambda: {name} 준비됨", file=sys.stderr)
        return name
    except Exception as exc:  # graceful-off — 배포 중단 금지 (websearch 규약)
        print(f"⚠ pricing_lambda 프로비저닝 실패 — {exc}. "
              "LITELLM_PRICING_LAMBDA 미주입으로 진행하면 litellm 소스는 "
              "비활성 상태가 됩니다", file=sys.stderr)
        return ""


def teardown(cfg, admin_role_arn: str = "", detach_invoke: bool = True) -> bool:
    """정책→함수→role 역순 삭제 (멱등). 반환 성공 여부."""
    region = cfg.aws.region
    name = fn_name(cfg.env)
    ok = True
    try:
        lam, iam, _ = _clients(region)
        if detach_invoke and admin_role_arn:
            try:
                iam.delete_role_policy(
                    RoleName=admin_role_arn.rsplit("/", 1)[-1],
                    PolicyName=INVOKE_POLICY)
            except iam.exceptions.NoSuchEntityException:
                pass
        try:
            lam.delete_function(FunctionName=name)
        except lam.exceptions.ResourceNotFoundException:
            pass
        try:
            iam.get_role(RoleName=name)
            try:
                iam.detach_role_policy(RoleName=name, PolicyArn=BASIC_EXEC)
            except iam.exceptions.NoSuchEntityException:
                pass
            for p in iam.list_role_policies(RoleName=name)["PolicyNames"]:
                iam.delete_role_policy(RoleName=name, PolicyName=p)
            iam.delete_role(RoleName=name)
        except iam.exceptions.NoSuchEntityException:
            pass
        print(f"pricing_lambda: {name} 제거 완료", file=sys.stderr)
    except Exception as exc:
        print(f"⚠ pricing_lambda teardown 실패 — {exc} "
              f"(수동: Lambda {name} / role {name} 삭제)", file=sys.stderr)
        ok = False
    return ok
