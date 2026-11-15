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
# api.litellm.ai/model_catalog 는 종료됐다 — canonical cost map
# (GitHub model_prices_and_context_window.json) cost-map 모드로 돌린다.
# update_function_configuration 의 Environment 는 전체 교체라 이 맵이 곧
# 함수 env 의 소스 오브 트루스다.
# main 이 아니라 릴리스 태그를 핀한다 — 상류 푸시가 단가 소스를 조용히
# 바꾸지 않게(재현 가능). 새 모델 반영이 필요하면 태그를 올린다.
FN_ENV = {
    "LITELLM_COST_MAP_URL":
        "https://raw.githubusercontent.com/BerriAI/litellm/v1.108.0/"
        "model_prices_and_context_window.json",
}
BASIC_EXEC = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
INVOKE_POLICY = "LitellmPricingInvoke"
# 소유 마커 — role/함수 이름은 계정-전역이라 같은 env 명을 쓰는 다른
# 배포와 충돌할 수 있다. 채택/삭제는 이 마커가 있는 리소스로 한정한다
# (websearch 프로비저너의 Description 마커와 같은 계약). 기존 배포가
# 만든 리소스도 이 문구를 Description 에 갖는다 — 하위호환.
_OWNED_MARK = "llm-gateway"

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
    """읽기 전용 상태 — plan/preview 에서도 안전.
    boto3 미설치 머신에서도 preview 가 죽지 않게 import 실패를 삼킨다."""
    fn, role, invoke = "미배포", "미생성", "미부여"
    try:
        lam, iam, _ = _clients(region)
    except Exception:
        return {"function": fn, "role": role, "invoke": invoke}
    try:
        f = lam.get_function(FunctionName=fn_name(env))
        # State=Failed/Pending 도 존재로 치면 preview 가 'Active' 로 거짓 표시
        st = ((f.get("Configuration") or {}).get("State")) or "Active"
        fn = st
    except Exception as e:
        # ResourceNotFound 만 '미배포' — 권한/스로틀/엔드포인트 오류를 '없음'으로
        # 오판하면 소비자(ecs_apply)가 살아 있는 함수의 주입을 지운다.
        code = str((getattr(e, "response", None) or {})
                   .get("Error", {}).get("Code") or "")
        fn = "미배포" if "ResourceNotFound" in code else "조회 실패"
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
        # 부착은 get_role 성공 여부와 무관하게 항상 — create 와 attach 사이에서
        # 죽은 부분 실행이 role 만 남기면 get_role 재사용 경로가 attach 를 영구
        # 스킵해 함수가 로그도 못 쓰는 상태로 수렴한다. attach 는 멱등이다.
        try:
            _role = iam.get_role(RoleName=name)["Role"]
            if _OWNED_MARK not in (_role.get("Description") or ""):
                # 마커 없는 같은 이름 role 은 다른 배포/작업 소유일 수 있다 —
                # trust 를 덮어쓰면 그 워크로드의 AssumeRole 이 죽는다.
                print(f"⚠ pricing_lambda: role {name} 은 이 도구 소유로 "
                      "표시돼 있지 않습니다 — trust/policy 변경을 건너뜁니다. "
                      "의도한 채택이면 role Description 에 'llm-gateway' 를 "
                      "포함시키거나 role 을 삭제하세요", file=sys.stderr)
                return ""
            # trust 도 수렴한다 — 외부에서 만든 같은 이름 role 이나 손상된
            # trust 를 영구히 두지 않는다.
            iam.update_assume_role_policy(RoleName=name, PolicyDocument=TRUST)
        except iam.exceptions.NoSuchEntityException:
            iam.create_role(
                RoleName=name,
                AssumeRolePolicyDocument=TRUST,
                Description="llm-gateway LiteLLM pricing catalog fetch proxy")
            iam.get_waiter("role_exists").wait(RoleName=name)
            print(f"pricing_lambda: role 생성 {name} (전파 대기)", file=sys.stderr)
            import time
            time.sleep(10)   # role→lambda 전파 — ops 스크립트와 같은 10s
        had_policies = {p["PolicyArn"] for p in
                        iam.list_attached_role_policies(RoleName=name)
                        .get("AttachedPolicies", [])}
        if BASIC_EXEC not in had_policies:
            iam.attach_role_policy(RoleName=name, PolicyArn=BASIC_EXEC)

        # ② 함수 — 코드는 매번 갱신 배포 (멱등)
        zipb = _zip(src)
        try:
            _fn = lam.get_function(FunctionName=name)["Configuration"]
            if _OWNED_MARK not in (_fn.get("Description") or ""):
                print(f"⚠ pricing_lambda: 함수 {name} 은 외부 소유로 보여 "
                      "코드/설정 갱신을 건너뜁니다 (Description 에 "
                      "'llm-gateway' 없음)", file=sys.stderr)
                return ""
            lam.update_function_code(FunctionName=name, ZipFile=zipb)
            lam.get_waiter("function_updated").wait(FunctionName=name)
            # Runtime 도 수렴한다 — 수동/레거시 배포로 남은 구버전 런타임은
            # ensure/status 어느 쪽에도 drift 로 표시되지 않는다.
            lam.update_function_configuration(
                FunctionName=name, Runtime=RUNTIME, Timeout=TIMEOUT,
                MemorySize=MEMORY, Environment={"Variables": FN_ENV})
        except lam.exceptions.ResourceNotFoundException:
            lam.create_function(
                FunctionName=name, Runtime=RUNTIME,
                Handler="lambda_function.lambda_handler",
                Role=role_arn, Code={"ZipFile": zipb},
                Timeout=TIMEOUT, MemorySize=MEMORY,
                Environment={"Variables": FN_ENV},
                Description="LiteLLM model_catalog fetch proxy "
                            "for llm-gateway admin-api")

        # ③ admin-api role 에 invoke 인라인 정책 (eks — ecs 는 모듈 소유)
        if attach_invoke and not admin_role_arn:
            print("⚠ pricing_lambda: admin_api_role_arn output 없음 — invoke "
                  "정책 미부여로 함수만 배포됩니다 (admin-api invoke 는 "
                  "AccessDenied)", file=sys.stderr)
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
    except Exception as exc:
        print(f"⚠ pricing_lambda teardown 실패 — {exc} "
              f"(수동: Lambda {name} / role {name} 삭제)", file=sys.stderr)
        return False

    def _step(label: str, fn) -> None:
        # 단계별 격리 — 한 리소스의 실패가 나머지 정리를 중단시키지 않는다
        # (함수만 남고 role 이 새는 부분 teardown 방지). 재실행은 멱등.
        nonlocal ok
        try:
            fn()
        except Exception as exc:
            # NotFound 계열은 '이미 정리됨' — 성공으로 본다. 타입으로 먼저
            # 보고(boto3 예외 클래스), 코드도 함께 본다(래핑된 오류).
            notfound = isinstance(
                exc, (iam.exceptions.NoSuchEntityException,
                      lam.exceptions.ResourceNotFoundException))
            code = getattr(exc, "response", {}).get("Error", {}).get("Code", "")
            if not notfound and code not in (
                    "NoSuchEntity", "ResourceNotFoundException",
                    "ResourceNotFound"):
                ok = False
                print(f"⚠ pricing_lambda teardown {label} 실패 — {exc}",
                      file=sys.stderr)

    if detach_invoke and admin_role_arn:
        _step("invoke 정책", lambda: iam.delete_role_policy(
            RoleName=admin_role_arn.rsplit("/", 1)[-1],
            PolicyName=INVOKE_POLICY))
    def _delete_fn():
        _fn = lam.get_function(FunctionName=name)["Configuration"]
        if _OWNED_MARK not in (_fn.get("Description") or ""):
            print(f"⚠ pricing_lambda: 함수 {name} 은 외부 소유로 보여 "
                  "삭제하지 않습니다", file=sys.stderr)
            return
        lam.delete_function(FunctionName=name)
    _step("함수", _delete_fn)

    def _delete_role():
        _role = iam.get_role(RoleName=name)["Role"]
        if _OWNED_MARK not in (_role.get("Description") or ""):
            # 마커 없는 role 의 인라인 정책/본체를 지우면 다른 배포가 죽는다.
            print(f"⚠ pricing_lambda: role {name} 은 외부 소유로 보여 "
                  "삭제하지 않습니다 (Description 에 'llm-gateway' 없음)",
                  file=sys.stderr)
            return
        try:
            iam.detach_role_policy(RoleName=name, PolicyArn=BASIC_EXEC)
        except iam.exceptions.NoSuchEntityException:
            pass
        for page in iam.get_paginator("list_role_policies").paginate(
                RoleName=name):
            for p in page["PolicyNames"]:
                iam.delete_role_policy(RoleName=name, PolicyName=p)
        iam.delete_role(RoleName=name)
    _step("role", _delete_role)
    if ok:
        print(f"pricing_lambda: {name} 제거 완료", file=sys.stderr)
    return ok
