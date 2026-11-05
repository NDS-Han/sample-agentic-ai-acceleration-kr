# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""본문 로깅 IAM 스코프 회귀 테스트.

본문 버킷에는 마스킹되지 않은 프롬프트가 누적되므로 권한 형태가 보안 계약이다:

* gateway-proxy — 쓰기 전용(Firehose Put + S3 fallback PutObject). 읽기가
  생기면 파드 침해가 곧 전체 이력 유출이 된다.
* admin-api — 본문 뷰어가 관리자 대신 읽으므로 GetObject/ListBucket 을
  **body-log 버킷 한정**으로 연다. 버킷이 SSE-KMS 면 kms:Decrypt 도 필요하고,
  ViaService=s3 로 좁혀야 한다.

.tf 텍스트를 policy document 블록 단위로 잘라 검사한다 — terraform plan 없이
구조 회귀를 잡는 가벼운 검사다.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
IRSA_TF = REPO_ROOT / "deployment/terraform/modules/irsa/main.tf"
ECS_IAM_TF = REPO_ROOT / "deployment/terraform/modules/ecs-gateway/iam.tf"


def _policy_block(text: str, name: str) -> str:
    """`data "aws_iam_policy_document" "<name>" { ... }` 블록만 추출한다."""
    m = re.search(
        rf'data "aws_iam_policy_document" "{name}" \{{', text
    )
    assert m, f"{name} policy document 가 없다"
    start = m.end()
    depth, i = 1, start
    while depth and i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[start:i]


def test_proxy_body_log_write_only() -> None:
    """bedrock(proxy) 정책 — 본문 sink 에 Put 계열만, Get/List/Delete 없다."""
    block = _policy_block(IRSA_TF.read_text(), "bedrock")
    # 이 블록에서 본문 관련 statement 만 추린다 — 다른 statement(Bedrock invoke
    # 등)에 읽기 액션이 있어도 본문 버킷과 무관하므로 sid 로 한정한다.
    body_log = re.findall(
        r"sid\s*=\s*\"(BodyLog[^\"]+)\".*?actions\s*=\s*\[(.*?)\]",
        block, re.S,
    )
    sids = {sid for sid, _ in body_log}
    assert "BodyLogFirehoseWrite" in sids
    assert "BodyLogS3Fallback" in sids
    for sid, actions in body_log:
        assert "GetObject" not in actions, f"{sid} 에 읽기 권한이 생겼다"
        assert "ListBucket" not in actions, f"{sid} 에 목록 권한이 생겼다"
        assert "DeleteObject" not in actions
    # fallback 은 PutObject 단독이어야 한다
    fallback = dict(body_log)["BodyLogS3Fallback"]
    assert "s3:PutObject" in fallback


def test_admin_api_read_scoped_to_body_log_bucket() -> None:
    """admin_api 정책 — 읽기는 body_log 버킷 ARN 한정(`/*` 객체 + 버킷 List)."""
    block = _policy_block(IRSA_TF.read_text(), "admin_api")
    assert "BodyLogObjectRead" in block
    assert "BodyLogBucketList" in block
    read = re.search(
        r'sid\s*=\s*"BodyLogObjectRead".*?resources\s*=\s*\[(.*?)\]',
        block, re.S,
    )
    assert read and "body_log_bucket_arn" in read.group(1)
    # 전체 버킷 열람 방지 — wildcard 리소스가 아니라 버킷 ARN 변수여야 한다
    assert '"*"' not in read.group(1)
    lst = re.search(
        r'sid\s*=\s*"BodyLogBucketList".*?resources\s*=\s*\[(.*?)\]',
        block, re.S,
    )
    assert lst and "body_log_bucket_arn" in lst.group(1)


def test_admin_api_kms_decrypt_viaservice_scoped() -> None:
    """SSE-KMS 버킷 대비 kms:Decrypt — ViaService=s3 조건이 없으면 키가
    다른 서비스 경유 복호화에도 쓰일 수 있다."""
    block = _policy_block(IRSA_TF.read_text(), "admin_api")
    kms = re.search(
        r'sid\s*=\s*"BodyLogKmsDecrypt".*?kms:ViaService.*?\]',
        block, re.S,
    )
    assert kms, "BodyLogKmsDecrypt statement 또는 ViaService 조건이 없다"
    assert "s3." in kms.group(0) and "amazonaws.com" in kms.group(0)


def test_ecs_admin_task_same_scope() -> None:
    """ECS 경로 admin task role 도 같은 읽기 스코프를 가져야 한다(EKS 와 동등)."""
    text = ECS_IAM_TF.read_text()
    assert "BodyLogObjectRead" in text
    assert "BodyLogBucketList" in text
    assert "BodyLogKmsDecrypt" in text
    # kms statement 가 s3 ViaService 조건을 가진다
    kms = re.search(
        r'sid\s*=\s*"BodyLogKmsDecrypt".*?kms:ViaService.*?\]', text, re.S
    )
    assert kms
