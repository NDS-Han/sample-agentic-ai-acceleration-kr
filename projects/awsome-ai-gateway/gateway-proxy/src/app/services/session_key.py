# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""클라이언트 대화 세션 키 추출 — usage_logs.session_id 귀속용.

Anthropic Messages `metadata.user_id` 의 ``session_<id>`` 접미(Claude Code 가
``user_<hash>_account_<uuid>_session_<uuid>`` 형태로 보낸다)와
``metadata.session_id`` 직접 필드를 인식한다. 둘 다 없으면 None — 관리자 세션
뷰어는 NULL 행을 시간 인접성 휴리스틱으로 폴백 그룹핑한다.

⚠️ 프록시가 provider 로 보내는 본문의 metadata.user_id 는 sso_subject 로
덮어쓰므로, **원본 클라이언트 본문**(req_data/body_dict)에서만 추출해야 한다.
"""
import re

# ``session_`` 접미 — Claude Code 형식 외에도 "…_session_<id>" 패턴은 전부 인식.
_SESSION_SUFFIX = re.compile(r"(?:^|_)session_([0-9A-Za-z-]{8,128})")
# 반환값은 클라이언트 제어 문자열이므로 asyncpg/JSON 안전 문자만 허용한다 —
# NUL 이면 INSERT 가 CharacterNotInRepertoireError 로 행 전체가 유실되고
# (워커가 영구오류로 스킵 → usage_logs·budget_usages 둘 다 누락),
# lone surrogate 는 model_dump_json 이 PydanticSerializationError 로 죽어
# XADD/스풀 전에 종결된다. 화이트리스트가 아니면 None 으로 버린다.
_ALLOWED = re.compile(r"[0-9A-Za-z._:-]{1,128}")


def extract_session_id(body: dict | None) -> str | None:
    """원본 요청 본문에서 대화 세션 키를 뽑는다. 없으면 None."""
    if not isinstance(body, dict):
        return None
    md = body.get("metadata")
    if not isinstance(md, dict):
        return None
    sid = md.get("session_id")
    if isinstance(sid, str) and _ALLOWED.fullmatch(sid):
        return sid
    uid = md.get("user_id")
    if not isinstance(uid, str):
        return None
    m = _SESSION_SUFFIX.search(uid)
    return m.group(1) if m else None
