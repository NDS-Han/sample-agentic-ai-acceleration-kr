# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""session_key.extract_session_id — 원본 본문 metadata 에서 대화 세션 키 추출."""
from app.services.session_key import extract_session_id

_CLAUDE_CODE_UID = (
    "user_9f3a2c1b_account_11111111-2222-3333-4444-555555555555"
    "_session_abcdef01-2345-6789-abcd-ef0123456789"
)


def test_claude_code_session_suffix():
    body = {"metadata": {"user_id": _CLAUDE_CODE_UID}}
    assert (
        extract_session_id(body) == "abcdef01-2345-6789-abcd-ef0123456789"
    )


def test_explicit_metadata_session_id():
    body = {"metadata": {"session_id"}}
    # dict 래퍼 없이 직접 문자열 필드 — 호출부에서 문자열이 들어온다.
    body = {"metadata": {"session_id": "conv-42"}}
    assert extract_session_id(body) == "conv-42"


def test_no_metadata_returns_none():
    assert extract_session_id({}) is None
    assert extract_session_id(None) is None
    assert extract_session_id({"metadata": None}) is None
    assert extract_session_id({"metadata": "str"}) is None


def test_user_id_without_session_suffix_returns_none():
    """session_ 접미 없는 metadata.user_id(프록시 주입 sso_subject 등)는 묶지 않는다."""
    assert extract_session_id({"metadata": {"user_id": "sso-subject-1"}}) is None


def test_non_string_values():
    assert extract_session_id({"metadata": {"user_id": 123}}) is None
    assert extract_session_id({"metadata": {"user_id": {"a": 1}}}) is None


def test_session_id_truncated_to_128():
    body = {"metadata": {"session_id": "x" * 300}}
    got = extract_session_id(body)
    assert got is not None and len(got) == 128
