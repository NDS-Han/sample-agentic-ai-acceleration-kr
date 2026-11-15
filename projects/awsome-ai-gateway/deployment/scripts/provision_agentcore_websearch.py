#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""Provision the AgentCore Gateway + managed WebSearch target for gateway-proxy.

Architecture C: gateway-proxy is the MCP *caller* (SigV4/IRSA) → this gateway →
managed WebSearch connector. Inbound auth is AWS_IAM (SigV4), NOT Cognito — the pod
has IRSA creds, so no JWT/Cognito is needed (differs from the sibling
gonsoomoon-ml/web-search-mcp which used CUSTOM_JWT because CoWork can't SigV4).

Idempotent (re-run safe): reuses existing role / gateway / target. The managed
WebSearch connector is us-east-1-only and billed ~$7/1,000 queries (idle ≈ $0).

Subcommands:
    deploy    (default) create/reuse IAM exec role + Gateway(AWS_IAM) + WebSearch target,
              then print AGENTCORE_GATEWAY_URL / AGENTCORE_TARGET_ID for helm values.
    status    print current resource state.
    teardown  delete target → gateway → exec role (stop billing/exposure).

The SDK may not yet model the `mcp.connector` targetConfiguration; on ParamValidationError
we fall back to a raw SigV4 POST to the control-plane REST endpoint (verified pattern).

Env overrides: REGION (default us-east-1), GW_NAME, ROLE_NAME, TARGET_NAME,
CALLER_ROLE_ARN (the gateway-proxy IRSA role allowed to InvokeGateway; informational).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest
from botocore.exceptions import ClientError, ParamValidationError

REGION = os.environ.get("REGION", "us-east-1")  # WebSearch connector is us-east-1 only
# ⚠️ 이미 게이트웨이를 만들어 둔 환경에서 다시 실행할 때는 `GW_NAME` 을 **그때 쓴 이름과
#    똑같이** 넘겨야 한다. find_gateway() 가 이름 완전일치로 찾기 때문에, 이름이 다르면
#    기존 것을 재사용하지 않고 중복 게이트웨이를 하나 더 만들고, values 의
#    AGENTCORE_GATEWAY_URL 은 여전히 옛 호스트를 가리킨 채로 남는다(그래서 새로 만든
#    쪽은 아무도 쓰지 않는다). 이름을 바꾸려면 values 의 URL 도 함께 갱신할 것.
GW_NAME = os.environ.get("GW_NAME", "awsome-ai-gw-websearch")
ROLE_NAME = os.environ.get("ROLE_NAME", "llm-gateway-dev-agentcore-websearch-gw")
TARGET_NAME = os.environ.get("TARGET_NAME", "web-search-tool")
CONNECTOR_ID = "web-search"
TOOL_CONFIG_NAME = "WebSearch"
WEB_SEARCH_TOOL_ARN = f"arn:aws:bedrock-agentcore:{REGION}:aws:tool/web-search.v1"


def log(msg: str) -> None:
    print(f"[provision] {msg}", file=sys.stderr)


def fail(msg: str) -> None:
    print(f"[provision] ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _control():
    return boto3.client("bedrock-agentcore-control", region_name=REGION)


def account_id() -> str:
    return boto3.client("sts").get_caller_identity()["Account"]


# ── IAM exec role (gateway → WebSearch) ───────────────────────────────────────
def ensure_role(acct: str, dry: bool) -> str:
    arn = f"arn:aws:iam::{acct}:role/{ROLE_NAME}"
    if dry:
        log(f"[dry-run] IAM exec role {ROLE_NAME} (InvokeGateway + InvokeWebSearch)")
        return arn
    iam = boto3.client("iam")
    trust = {"Version": "2012-10-17", "Statement": [{
        "Sid": "AllowAgentCore", "Effect": "Allow",
        "Principal": {"Service": "bedrock-agentcore.amazonaws.com"}, "Action": "sts:AssumeRole",
        "Condition": {"StringEquals": {"aws:SourceAccount": acct},
                      "ArnLike": {"aws:SourceArn": f"arn:aws:bedrock-agentcore:{REGION}:{acct}:gateway/*"}}}]}
    # The web-search connector doc places BOTH actions on the gateway service role.
    perms = {"Version": "2012-10-17", "Statement": [
        {"Sid": "InvokeGateway", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeGateway",
         "Resource": f"arn:aws:bedrock-agentcore:{REGION}:{acct}:gateway/*"},
        {"Sid": "InvokeWebSearch", "Effect": "Allow", "Action": "bedrock-agentcore:InvokeWebSearch",
         "Resource": WEB_SEARCH_TOOL_ARN}]}
    try:
        iam.create_role(RoleName=ROLE_NAME, AssumeRolePolicyDocument=json.dumps(trust),
                        Description="AgentCore Gateway exec role for managed Web Search")
        log(f"IAM role created: {ROLE_NAME}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "EntityAlreadyExists":
            log(f"IAM role reused: {ROLE_NAME}")
            # 재사용 시 trust 도 수렴한다 — 예전 실행이 조건 없이 만든 role 을
            # 영구히 그대로 두지 않는다(put_role_policy 와 같은 멱등 갱신).
            # 단, 다른 용도로 만든 동명 role 이 있을 수 있으니 소유 표식이
            # 다르면 경고를 남긴다(teardown 도 이 표식으로 역삭제를 게이트).
            try:
                desc = iam.get_role(RoleName=ROLE_NAME)["Role"].get("Description", "")
            except ClientError:
                desc = ""
            if desc != "AgentCore Gateway exec role for managed Web Search":
                log(f"WARN: {ROLE_NAME} 의 Description 이 프로비저너 소유 표식과 "
                    f"다릅니다({desc!r}) — 수동/외부 생성 role 을 채택합니다")
            iam.update_assume_role_policy(
                RoleName=ROLE_NAME, PolicyDocument=json.dumps(trust))
        else:
            raise
    iam.put_role_policy(RoleName=ROLE_NAME, PolicyName="web-search-invoke",
                        PolicyDocument=json.dumps(perms))
    return arn


# ── Gateway (AWS_IAM inbound) ─────────────────────────────────────────────────
def find_gateway():
    gw = _control()
    for page in gw.get_paginator("list_gateways").paginate():
        g = next((x for x in page.get("items", []) if x.get("name") == GW_NAME), None)
        if g:
            try:
                return gw.get_gateway(gatewayIdentifier=g["gatewayId"])
            except ClientError as e:
                # list ↔ get 사이에 삭제된 경우 — 다음 목록으로 수렴한다.
                if e.response["Error"]["Code"] in (
                        "ResourceNotFoundException", "ResourceNotFound"):
                    continue
                raise
    return None


def ensure_gateway(role_arn: str, dry: bool):
    existing = find_gateway()
    if existing and existing.get("status") in (
            "FAILED", "CREATE_FAILED", "UPDATE_FAILED",
            "DELETING", "DELETED"):
        # 죽은 gateway 는 이름 충돌로 create 를 막는다 — 그대로 재사용하면
        # wait_status 가 매번 실패해 수동 삭제 전까지 영구 데드락.
        gw_id = existing["gatewayId"]
        log(f"Gateway {existing.get('status')} 상태 — 재생성을 위해 삭제: {gw_id}")
        if not dry:
            if existing.get("status") != "DELETING":
                _control().delete_gateway(gatewayIdentifier=gw_id)
            for _ in range(45):
                if find_gateway() is None:
                    break
                time.sleep(4)
            else:
                fail("Gateway 삭제 전파 타임아웃 — 콘솔에서 삭제 후 재시도하세요")
        existing = None
    if existing:
        # 이름만으로 채택하기 전에 우리가 만든 형태인지 확인한다 — 동명의
        # CUSTOM_JWT gateway 를 채택하면 상태는 READY 인데 호출이 전부 403.
        if existing.get("authorizerType") != "AWS_IAM":
            fail(f"Gateway {GW_NAME} 이 이미 있지만 authorizerType="
                 f"{existing.get('authorizerType')} — 외부 소유로 간주하고 중단합니다. "
                 "삭제하거나 이름을 바꾼 뒤 재실행하세요")
        if existing.get("roleArn") and existing["roleArn"] != role_arn:
            log(f"WARN: Gateway roleArn({existing['roleArn']}) 이 이번 실행의 "
                f"role({role_arn}) 과 다릅니다 — gateway 는 재사용하고 role 은 "
                "건드리지 않습니다")
        log(f"Gateway reused: {existing['gatewayId']} ({existing.get('status')})")
        return existing["gatewayId"], existing["gatewayUrl"]
    if dry:
        log(f"[dry-run] Gateway {GW_NAME} (protocolType=MCP, authorizerType=AWS_IAM)")
        return "<dry-gateway-id>", "<dry-gateway-url>"
    r = _control().create_gateway(
        name=GW_NAME, roleArn=role_arn, protocolType="MCP", authorizerType="AWS_IAM",
        description="Managed WebSearch for awsome-ai-gateway (Architecture C, SigV4 inbound)",
    )
    log(f"Gateway created: {r['gatewayId']}")
    return r["gatewayId"], r["gatewayUrl"]


def wait_status(getter, label: str, ok="READY", bad=("FAILED", "DELETING", "DELETED"), n=75, p=4):
    last = "?"
    for _ in range(n):
        last = getter()
        if last == ok:
            log(f"{label} {ok}")
            return
        if last in bad:
            fail(f"{label} bad status: {last}")
        time.sleep(p)
    fail(f"{label} timeout (last={last})")


# ── WebSearch connector target (boto3 → SigV4 fallback) ───────────────────────
def find_target(gw_id: str):
    gw = _control()
    for page in gw.get_paginator("list_gateway_targets").paginate(
            gatewayIdentifier=gw_id):
        t = next((x for x in page.get("items", []) if x.get("name") == TARGET_NAME), None)
        if t:
            return t
    return None


def ensure_target(gw_id: str, dry: bool) -> str:
    if dry:
        log(f"[dry-run] WebSearch connector target (connectorId={CONNECTOR_ID})")
        return "<dry-target-id>"
    ex = find_target(gw_id)
    if ex and ex.get("status") in ("FAILED", "CREATE_FAILED",
                                 "UPDATE_FAILED", "DELETING", "DELETED"):
        # 죽은 target 은 재사용하면 검색 도구가 영구 실패 — 삭제 후 재생성.
        log(f"Target {ex.get('status')} 상태 — 재생성을 위해 삭제: {ex['targetId']}")
        if ex.get("status") != "DELETING":
            _control().delete_gateway_target(gatewayIdentifier=gw_id,
                                             targetId=ex["targetId"])
        for _ in range(30):
            if find_target(gw_id) is None:
                break
            time.sleep(2)
        else:
            fail("Target 삭제 전파 타임아웃 — 콘솔에서 삭제 후 재시도하세요")
        ex = None
    if ex:
        log(f"Target reused: {ex['targetId']} ({ex.get('status')})")
        return ex["targetId"]
    target_cfg = {"mcp": {"connector": {"source": {"connectorId": CONNECTOR_ID},
                  "configurations": [{"name": TOOL_CONFIG_NAME, "parameterValues": {}}]}}}
    creds = [{"credentialProviderType": "GATEWAY_IAM_ROLE"}]
    gw = _control()
    try:
        r = gw.create_gateway_target(gatewayIdentifier=gw_id, name=TARGET_NAME,
                                     targetConfiguration=target_cfg,
                                     credentialProviderConfigurations=creds)
        log(f"Target created (boto3): {r['targetId']}")
        return r["targetId"]
    except ParamValidationError:
        log("boto3 SDK does not model 'connector' → raw SigV4 fallback")
        return _create_target_sigv4(gw_id, target_cfg, creds)


def _create_target_sigv4(gw_id: str, target_cfg: dict, creds: list) -> str:
    fr = boto3.Session().get_credentials().get_frozen_credentials()
    url = f"https://bedrock-agentcore-control.{REGION}.amazonaws.com/gateways/{gw_id}/targets/"
    body = json.dumps({"name": TARGET_NAME, "clientToken": str(uuid.uuid4()),
                       "targetConfiguration": target_cfg,
                       "credentialProviderConfigurations": creds}).encode()
    req = AWSRequest(method="POST", url=url, data=body, headers={"Content-Type": "application/json"})
    SigV4Auth(fr, "bedrock-agentcore", REGION).add_auth(req)
    p = req.prepare()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(p.url, data=body, headers=dict(p.headers), method="POST"),
            timeout=30,
        ) as r:
            tid = json.load(r)["targetId"]
            log(f"Target created (SigV4): {tid}")
            return tid
    except urllib.error.HTTPError as e:
        fail(f"SigV4 target create failed: HTTP {e.code} {e.read().decode()[:300]}")


# ── subcommands ───────────────────────────────────────────────────────────────
def cmd_deploy(dry: bool):
    acct = account_id()
    log(f"account={acct} region={REGION} gateway={GW_NAME}")
    role_arn = ensure_role(acct, dry)
    gw_id, gw_url = ensure_gateway(role_arn, dry)
    if not dry:
        wait_status(lambda: _control().get_gateway(gatewayIdentifier=gw_id).get("status", "?"), "gateway")
    tid = ensure_target(gw_id, dry)
    if not dry:
        wait_status(lambda: (find_target(gw_id) or {}).get("status", "?"), "target")
    mcp_url = gw_url if gw_url.endswith("/mcp") else gw_url.rstrip("/") + "/mcp"
    print()
    print(f"AGENTCORE_GATEWAY_URL={mcp_url}")
    print(f"AGENTCORE_TARGET_ID={TARGET_NAME}")
    print(f"AGENTCORE_REGION={REGION}")
    if not dry:
        log("Set AGENTCORE_GATEWAY_URL in values-eks-fargate-dev.yaml then redeploy gateway-proxy.")
        log("Verify: python3 deployment/scripts/provision_agentcore_websearch.py status")


def cmd_status():
    g = find_gateway()
    if not g:
        print("gateway: NONE")
        return
    print(f"gateway: {g['gatewayId']} status={g.get('status')} authorizerType={g.get('authorizerType')}")
    print(f"  url: {g['gatewayUrl']}")
    t = find_target(g["gatewayId"])
    print(f"  target: {t['targetId']} status={t.get('status')}" if t else "  target: NONE")


def cmd_teardown():
    g = find_gateway()
    gw = _control()
    if g:
        t = find_target(g["gatewayId"])
        if t:
            try:
                if t.get("status") != "DELETING":
                    gw.delete_gateway_target(gatewayIdentifier=g["gatewayId"],
                                             targetId=t["targetId"])
                    log(f"target deleted: {t['targetId']}")
            except ClientError as e:
                # target 삭제가 실패해도 gateway/IAM 정리는 계속한다 —
                # 남은 target 은 다음 실행이 수렴.
                log(f"target 삭제 실패({e.response['Error']['Code']}) — 계속 진행")
            # target 삭제가 전파되기 전에 gateway 를 지우면 Conflict 계열 오류 —
            # 고정 sleep 대신 사라질 때까지 폴링한다.
            for _ in range(30):
                if find_target(g["gatewayId"]) is None:
                    break
                time.sleep(2)
        try:
            gw.delete_gateway(gatewayIdentifier=g["gatewayId"])
            log(f"gateway deleted: {g['gatewayId']}")
        except ClientError as e:
            log(f"gateway 삭제 실패({e.response['Error']['Code']}) — "
                "다음 실행이 수렴합니다")
    else:
        log("gateway NONE — skip")
    iam = boto3.client("iam")
    # 우리가 만든 role 만 지운다 — gateway 가 다른 role 을 참조하거나(수동
    # 배포), Description 이 프로비저너 표식과 다르면 외부 소유로 보고 손대지 않는다.
    role_ours = True
    expected = f"arn:aws:iam::{account_id()}:role/{ROLE_NAME}"
    if g and g.get("roleArn") and g["roleArn"] != expected:
        log(f"WARN: gateway 가 외부 role({g['roleArn']}) 을 참조 — "
            f"{ROLE_NAME} 삭제를 건너뜁니다")
        role_ours = False
    if role_ours:
        try:
            desc = iam.get_role(RoleName=ROLE_NAME)["Role"].get("Description", "")
            if desc != "AgentCore Gateway exec role for managed Web Search":
                log(f"WARN: {ROLE_NAME} 의 Description({desc!r}) 이 소유 표식과 "
                    "달라 삭제를 건너뜁니다")
                role_ours = False
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchEntity":
                log("IAM role NONE — skip")
                role_ours = False
            else:
                raise
    if role_ours:
        try:
            iam.delete_role_policy(RoleName=ROLE_NAME, PolicyName="web-search-invoke")
            iam.delete_role(RoleName=ROLE_NAME)
            log(f"IAM role deleted: {ROLE_NAME}")
        except ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchEntity":
                log("IAM role NONE — skip")
            else:
                raise
    log("teardown complete.")


def main():
    ap = argparse.ArgumentParser(description="Provision AgentCore Gateway + WebSearch target")
    ap.add_argument("command", nargs="?", default="deploy", choices=["deploy", "status", "teardown"])
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if args.command == "deploy":
        cmd_deploy(args.dry_run)
    elif args.command == "status":
        cmd_status()
    elif args.command == "teardown":
        cmd_teardown()


if __name__ == "__main__":
    main()
