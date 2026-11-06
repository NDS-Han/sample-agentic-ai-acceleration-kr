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
    body = {"metadata": {"session_id": "conv-42"}}
    assert extract_session_id(body) == "conv-42"


def test_explicit_session_id_rejects_nul_and_surrogate():
    """NUL → asyncpg 22021 로 usage 행 유실, lone surrogate → dump 실패 — 전부 None."""
    assert extract_session_id({"metadata": {"session_id": "a\x00b"}}) is None
    assert extract_session_id({"metadata": {"session_id": "a\ud800b"}}) is None
    assert extract_session_id({"metadata": {"session_id": "  "}}) is None
    assert extract_session_id({"metadata": {"session_id": "한글세션"}}) is None


def test_empty_session_id_falls_back_to_user_id():
    body = {
        "metadata": {
            "session_id": "",
            "user_id": "user_x_account_y_session_abcdef01-2345-6789-abcd-ef0123456789",
        }
    }
    assert extract_session_id(body) == "abcdef01-2345-6789-abcd-ef0123456789"


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


def test_session_id_over_128_rejected():
    """128자 초과는 접두 절단하면 서로 다른 세션이 충돌할 수 있어 통째로 거부."""
    body = {"metadata": {"session_id": "x" * 300}}
    assert extract_session_id(body) is None
    body = {"metadata": {"session_id": "x" * 128}}
    assert extract_session_id(body) == "x" * 128
