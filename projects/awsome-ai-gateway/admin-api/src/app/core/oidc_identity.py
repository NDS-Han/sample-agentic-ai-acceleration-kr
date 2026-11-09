# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""IdP 클레임 → 애플리케이션 신원 변환 정책, **한 곳에서만**.

이 모듈이 왜 따로 있나: 같은 정책이 두 경로에서 필요하다.

  1. ``POST /v1/auth/exchange`` — CLI 가 IdP 토큰을 VK 로 바꾼다
     (``services/oidc_service.py``, 프로비저닝까지 함).
  2. ``admin_jwt`` 쿠키로 들어오는 admin-ui 세션 — admin-api 가 **리소스 서버**로서
     IdP id_token 을 읽어 신원을 세운다(``core/auth.py``).

두 경로가 각자 정책을 들고 있으면 조용히 어긋난다. 실제로 그 부류의 사고가
이 레포에 있었다: admin-ui 가 관리자 그룹 이름을 하드코딩하고 admin-api 는 설정
가능하게 두어서, 그룹을 개명하는 순간 API 는 인가하는데 UI 는 DEVELOPER 로
판정해 모든 페이지가 /403 이 됐다(관리자 잠김).

⚠️ 여기에는 **DB 접근도, 프로비저닝도 넣지 않는다.** 순수 함수만 둔다 —
   그래야 두 호출자가 각자의 트랜잭션·프로비저닝 정책을 유지할 수 있다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.config import get_settings
from app.models.auth import UserRole


@dataclass(frozen=True)
class OIDCClaims:
    """설정된 claim 이름으로 뽑아낸 값들."""

    subject: str
    email: str
    name: str
    groups: list[str]


def extract_claims(claims: dict) -> OIDCClaims:
    """IdP 별 claim 이름 차이를 흡수한다(Cognito 면 groups 가 ``cognito:groups``).

    ⚠️ ``groups`` 를 리스트로 정규화하는 것이 load-bearing 이다. 어떤 IdP 는 그룹이
       하나일 때 문자열로 준다(Azure AD 의 일부 설정, Keycloak 의 단일 값 매퍼).
       그때 ``in`` 검사를 그대로 하면 **부분 문자열 매칭**이 된다 —
       ``"GatewayAdminReadOnly"`` 가 ``"GatewayAdmin"`` 을 포함하므로 읽기 전용
       그룹이 ADMIN 으로 승격된다. 리스트로 감싸면 원소 동등 비교가 된다.
    """
    settings = get_settings()

    subject = claims.get(settings.OIDC_USER_ID_CLAIM) or ""
    email = claims.get(settings.OIDC_EMAIL_CLAIM) or ""
    name = claims.get(settings.OIDC_NAME_CLAIM) or email or subject

    return OIDCClaims(
        subject=str(subject),
        email=str(email),
        name=str(name),
        groups=flatten_groups(claims.get(settings.OIDC_GROUPS_CLAIM)),
    )


def flatten_groups(raw) -> list[str]:
    """그룹 클레임/속성 값 → 그룹명 리스트.

    - 배열 클레임(cognito:groups): ["a","b"] → 그대로
    - 문자열(custom:groups 등 custom attribute): 단일 문자열만 저장되므로
      IdP 는 그룹 여러 개를 하나로 이어 보낸다 — 이어붙이는 구분자가 구현별로
      ';'/',' 로 갈려 둘 다 푼다. 단일 값은 split 이 1-요소를 만들어 호환된다.
    - 그 외(숫자·dict 등): 문자열화해 같은 규칙 적용.
    """
    if not raw:
        return []
    items = raw if isinstance(raw, list) else [raw]
    return [g.strip() for x in items
            for g in re.split(r"[;,]", str(x)) if g.strip()]


def team_resolution_groups(groups: list[str]) -> list[str]:
    """팀 해석용 정렬 — ADMIN_GROUPS 멤버를 맨 뒤로.

    "SSIR_Claude_Admin;SSIR_Claude_Users" 처럼 관리자+팀 그룹을 같이 들고 온
    사용자의 팀이 클레임 순서가 아니라 팀 그룹으로 결정되게 한다. 순수
    관리자(관리 그룹만 보유)는 뒤로 밀려도 자기 그룹으로 파싱되므로 영향 없다.
    """
    admin = set(get_settings().ADMIN_GROUPS)
    return ([g for g in groups if g not in admin]
            + [g for g in groups if g in admin])


def claims_truthy(v) -> bool:
    """JWT 클레임/Cognito 속성의 불리언 표현 흡수 — true, "true", "True", 1, "1"."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return v == 1
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return False


def derive_role(email: str, groups: list[str], *, email_verified: bool = False) -> UserRole:
    """``ADMIN_EMAILS`` / ``ADMIN_GROUPS`` 중 하나라도 맞으면 ADMIN, 아니면 DEVELOPER.

    이메일 비교는 대소문자 무시(주소는 대소문자 구분이 없다). 그룹 비교는 **정확히
    일치**해야 한다 — 그룹 이름은 IdP 의 식별자이고, 느슨하게 비교하면 위 주석의
    부분 문자열 승격이 그대로 재현된다.

    ``email_verified`` — ADMIN_EMAILS 매칭의 전제. 이메일 클레임은 사용자가
    쓸 수 있는 값이라(IdP 의 검증 요구 설정에 따라) 미검증 주소를 ADMIN 목록과
    매칭하면 주장만으로 권한상승이 된다. 그룹 클레임은 IdP 가 관리하므로
    이 게이트와 무관하다. Cognito 풀 속성(AdminGetUser 의 email_verified)은
    서버측 값이라 검증 근거로 쓸 수 있다. DB 에 저장된 이메일은 쓰지 않는다 —
    게이트 도입 이전에 미검증으로 저장된 행이 남아 있을 수 있다.
    """
    settings = get_settings()

    admin_emails = {e.lower() for e in settings.ADMIN_EMAILS}
    if email and email_verified and email.lower() in admin_emails:
        return UserRole.ADMIN

    admin_groups = set(settings.ADMIN_GROUPS)
    if any(g in admin_groups for g in groups):
        return UserRole.ADMIN

    return UserRole.DEVELOPER


def admin_mapping_configured() -> bool:
    """ADMIN 권한 출처가 IdP 클레임(그룹/이메일 목록)으로 설정된 운영 형태인지.

    True 면 토큰 클레임이 ADMIN 판정의 원천이다 — 그룹/이메일 목록에서 빠지는
    순간 권한 회수가 반영돼야 한다. False(둘 다 미설정)면 ``derive_role`` 이
    ADMIN 을 낼 수 없으므로 DB role 이 유일한 원천 — 수동 부여 운영 형태라
    클레임 기준 강등을 하면 관리자가 전원 잠긴다.
    """
    settings = get_settings()
    return bool(settings.ADMIN_GROUPS or settings.ADMIN_EMAILS)


def required_group_missing(groups: list[str]) -> str | None:
    """``OIDC_REQUIRED_GROUP`` 게이트. 통과면 None, 막히면 없는 그룹 이름.

    비어 있으면(기본값) 게이트가 없다 — 모든 인증 사용자가 통과한다.
    """
    settings = get_settings()
    required = settings.OIDC_REQUIRED_GROUP
    if required and required not in groups:
        return required
    return None
