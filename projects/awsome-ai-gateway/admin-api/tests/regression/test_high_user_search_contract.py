# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``GET /admin/users/search`` — 조직 트리 검색창의 백엔드 계약.

기존 ``GET /admin/users?email=`` 는 Cognito username **exact** 매칭(단건 조회)이라
검색창에 쓸 수 없다. 이 엔드포인트는 email/display_name 부분 매칭이고, 타이핑
한 글자마다 호출된다.

이 파일이 고정하는 네 가지 — 각각 없으면 조용히 잘못 동작한다:

  1. **라우트 선언 순서.** FastAPI 는 선언 순서로 매칭하므로 ``/users/search`` 가
     ``/users/{user_id}/...`` 뒤에 있으면 "search" 가 user_id 로 잡혀 422 가 난다.
     이건 코드를 읽어서는 눈에 띄지 않고 실행해야 드러난다.

  2. **LIKE 와일드카드 이스케이프.** ``%`` 한 글자로 전체 사용자가 매칭된다 —
     전량 스캔 + 무의미한 결과. ``_`` 는 임의의 한 글자라 조용한 오탐을 만든다.

  3. **잘림을 숨기지 않는다.** limit 에서 잘렸는데 알리지 않으면 사용자는 찾는
     사람이 없다고 결론 내린다.

  4. **컬럼-only SELECT.** ``select(User)`` 는 ``User.team``/``User.virtual_keys``
     를 eager-load 한다(둘 다 ``lazy="selectin"``). 타이핑마다 도는 경로에서 결과
     20건에 대해 팀·VK 추가 쿼리가 매번 붙는다.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.models.auth import UserRole
from app.repositories.user_repository import _escape_like
from app.services.user_team_service import UserTeamService

_ROUTERS = Path(__file__).resolve().parents[2] / "src" / "app" / "routers" / "users.py"
_REPO_PY = Path(__file__).resolve().parents[2] / "src" / "app" / "repositories" / "user_repository.py"


# ─────────────────────────────────────────────────────────────────────────────
# 1. LIKE 와일드카드 이스케이프
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("%", r"\%"),
        ("_", r"\_"),
        ("a%b", r"a\%b"),
        ("a_b", r"a\_b"),
        ("100%_x", r"100\%\_x"),
        # 백슬래시가 **먼저** 치환돼야 한다. 나중에 하면 앞서 넣은 이스케이프까지
        # 다시 이스케이프되어 리터럴 백슬래시가 와일드카드를 되살린다.
        ("\\", "\\\\"),
        ("a\\%b", r"a\\\%b"),
        # 평범한 문자는 건드리지 않는다(과잉 이스케이프는 검색을 못 하게 만든다).
        ("kim@example.com", "kim@example.com"),
        ("홍길동", "홍길동"),
        ("", ""),
    ],
)
def test_escape_like(raw, expected):
    assert _escape_like(raw) == expected


def test_escape_is_applied_and_escape_char_is_declared():
    """이스케이프 함수를 만들어 두고 쿼리에서 안 쓰면 아무 효과가 없다.

    그리고 ``escape="\\\\"`` 를 ilike 에 넘기지 않으면 PostgreSQL 은 백슬래시를
    이스케이프 문자로 보지 않으므로, 이스케이프한 문자열이 **리터럴 백슬래시**로
    검색된다 — 결과가 조용히 0건이 된다.
    """
    src = _REPO_PY.read_text(encoding="utf-8")
    body = src[src.index("    async def search_users(") :]
    body = body[: body.index("\n    async def ", 10)] if "\n    async def " in body[10:] else body

    assert "_escape_like(term)" in body, "search_users 가 이스케이프를 적용하지 않는다"
    ilikes = re.findall(r"\.ilike\(([^)]*)\)", body)
    assert ilikes, "ilike 호출을 찾지 못했다 — 구조가 바뀌었다"
    for call in ilikes:
        assert "escape=" in call, (
            f"ilike 에 escape 가 없다 — 이스케이프한 문자열이 리터럴로 검색된다: {call}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# 2. 라우트 선언 순서 (실행으로 판정)
# ─────────────────────────────────────────────────────────────────────────────


def _registered_paths() -> list[str]:
    """라우터에 등록된 경로를 **등록 순서대로**. prefix(``/admin``)가 붙어 있다."""
    from app.routers.users import router

    return [getattr(r, "path", "") for r in router.routes]


SEARCH_PATH = "/admin/users/search"


def test_search_route_is_registered():
    paths = _registered_paths()
    assert SEARCH_PATH in paths, f"검색 라우트가 등록되지 않았다: {paths[:8]}"


def test_search_is_declared_before_any_same_shape_dynamic_route():
    """``/users/search`` 와 **같은 형상**(세그먼트 수)의 동적 라우트보다 먼저여야 한다.

    ⚠️ 여기서 "같은 형상" 이 핵심이다. ``/users/{user_id}/team`` 은 세그먼트가 하나 더
       많아 ``/users/search`` 와 경쟁하지 않으므로, 그것보다 뒤에 있어도 무해하다.
       실제로 오늘 트리가 그 상태다 — 모든 ``{user_id}`` 라우트가 3세그먼트다.
       경쟁 대상은 ``/admin/users/{...}`` 딱 3세그먼트짜리 동적 라우트뿐이다.

    ⚠️ 그래서 이 테스트는 **오늘은 공허하다**(경쟁 라우트가 0개). 그 사실을 숨기지
       않고 아래에서 명시적으로 확인한다 — 나중에 ``GET /users/{user_id}`` 가
       추가되는 순간 이 검사가 살아나고, 그때 순서가 틀려 있으면 잡힌다.
    """
    paths = _registered_paths()
    i_search = paths.index(SEARCH_PATH)
    depth = SEARCH_PATH.count("/")

    competing = [
        i
        for i, p in enumerate(paths)
        if p.count("/") == depth and p.startswith("/admin/users/{") and i != i_search
    ]
    if competing:
        assert i_search < min(competing), (
            f"{SEARCH_PATH} 가 {i_search} 번째인데 같은 형상의 동적 라우트가 "
            f"{min(competing)} 번째에 있다 — 'search' 가 그 파라미터로 잡힌다"
        )


def test_no_same_shape_dynamic_route_exists_today():
    """위 테스트가 왜 오늘 공허한지를 기록으로 남긴다.

    이 단정이 실패하면(= 경쟁 라우트가 생겼다) 위 테스트가 실질 검사로 바뀌었다는
    뜻이다. 그때 이 테스트를 지우고 위 것만 남기면 된다.
    """
    paths = _registered_paths()
    depth = SEARCH_PATH.count("/")
    competing = [p for p in paths if p.count("/") == depth and p.startswith("/admin/users/{")]
    assert not competing, (
        f"같은 형상의 동적 라우트가 생겼다: {competing}. "
        "이제 선언 순서가 실제로 중요하다 — 위 테스트가 그것을 검사한다."
    )


def test_search_resolves_and_not_to_a_dynamic_route():
    """실제 라우팅으로 판정 — 정적 순서 비교가 놓치는 것을 잡는다.

    Starlette 라우터에 요청 스코프를 직접 물어 어느 엔드포인트가 매칭되는지 본다.
    이게 "search" 가 uuid 파라미터로 잡히는 사고를 유일하게 직접 증명하는 방법이다.
    """
    from starlette.routing import Match

    from app.routers.users import router

    scope = {"type": "http", "method": "GET", "path": SEARCH_PATH, "path_params": {}}
    matched = None
    for route in router.routes:
        m, child = route.matches(scope)
        if m == Match.FULL:
            matched = (route, child)
            break
    assert matched is not None, f"{SEARCH_PATH} 가 어떤 라우트에도 매칭되지 않는다"
    route, child = matched
    assert getattr(route, "path", "") == SEARCH_PATH, (
        f"{SEARCH_PATH} 가 {getattr(route, 'path', '?')} 로 매칭됐다"
    )
    # 동적 파라미터로 흡수되지 않았음을 직접 확인한다.
    assert not child.get("path_params"), (
        f"경로가 파라미터로 잡혔다: {child.get('path_params')}"
    )
    assert getattr(route.endpoint, "__name__", "") == "search_users"


# ─────────────────────────────────────────────────────────────────────────────
# 3. 서비스 계약 — 최소 길이 / 잘림 / 비활성 제외
# ─────────────────────────────────────────────────────────────────────────────


def _svc():
    from app.core.cache_invalidation import CacheInvalidationManager
    from app.services.key_service import KeyService

    cache_mgr = MagicMock(spec=CacheInvalidationManager)
    cache_mgr._redis = MagicMock()
    return UserTeamService(cache_mgr=cache_mgr, key_service=MagicMock(spec=KeyService))


def _rows(n: int):
    return [
        (uuid.uuid4(), f"u{i}@example.com", f"U{i}", UserRole.DEVELOPER, uuid.uuid4(), "T")
        for i in range(n)
    ]


def _patched(rows):
    repo = MagicMock()
    repo.search_users = AsyncMock(return_value=rows)
    return patch("app.services.user_team_service.UserRepository", return_value=repo), repo


@pytest.mark.parametrize("term", ["", " ", "a", " a "])
async def test_short_terms_never_touch_the_db(term):
    """1자 검색은 사실상 전량 매칭이다 — DB 를 치지 않아야 한다.

    ``strip()`` 후 판정하는 것도 중요하다: " a " 는 3자이지만 실제 검색어는 1자다.
    """
    ctx, repo = _patched(_rows(0))
    with ctx:
        items, truncated = await _svc().search_users(AsyncMock(), term=term)
    assert items == [] and truncated is False
    repo.search_users.assert_not_awaited(), f"{term!r} 로 DB 를 조회했다"


async def test_two_char_term_does_query():
    """대조군 — 위 단정이 "항상 조회 안 함" 이 아님을 보인다."""
    ctx, repo = _patched(_rows(1))
    with ctx:
        items, _ = await _svc().search_users(AsyncMock(), term="ki")
    assert len(items) == 1
    repo.search_users.assert_awaited_once()


async def test_repository_is_asked_for_limit_plus_one():
    """잘림 판정을 위해 limit+1 을 조회한다 — 별도 COUNT 쿼리를 피한다."""
    ctx, repo = _patched(_rows(3))
    with ctx:
        await _svc().search_users(AsyncMock(), term="kim", limit=5)
    kwargs = repo.search_users.await_args.kwargs
    assert kwargs["limit"] == 6, f"limit 이 {kwargs['limit']} — limit+1 이어야 한다"


async def test_truncated_is_reported_and_extra_row_is_dropped():
    ctx, _ = _patched(_rows(21))  # limit 20 → 21건 조회됨
    with ctx:
        items, truncated = await _svc().search_users(AsyncMock(), term="kim")
    assert truncated is True, "잘렸는데 알리지 않았다 — 사용자는 결과가 없다고 판단한다"
    assert len(items) == 20, f"{len(items)}건 반환 — limit+1 의 여분이 새어 나갔다"


async def test_exactly_limit_is_not_truncated():
    """경계 — 정확히 limit 이면 잘리지 않았다. off-by-one 이 흔한 자리다."""
    ctx, _ = _patched(_rows(20))
    with ctx:
        items, truncated = await _svc().search_users(AsyncMock(), term="kim")
    assert truncated is False
    assert len(items) == 20


async def test_only_active_users_are_requested():
    """검색 결과와 트리가 어긋나면 조상 경로를 펼칠 수 없다.

    트리는 ``active_members`` 로만 만들어지므로(get_org_tree), 비활성 사용자를
    검색 결과에 띄우면 선택했을 때 트리에서 찾지 못한다.
    """
    ctx, repo = _patched(_rows(1))
    with ctx:
        await _svc().search_users(AsyncMock(), term="kim")
    assert repo.search_users.await_args.kwargs["is_active"] is True


async def test_team_id_is_carried_through():
    """``team_id`` 가 응답에 있어야 UI 가 조상 경로를 펼칠 수 있다."""
    tid = uuid.uuid4()
    rows = [(uuid.uuid4(), "k@example.com", "K", UserRole.ADMIN, tid, "TeamX")]
    ctx, _ = _patched(rows)
    with ctx:
        items, _ = await _svc().search_users(AsyncMock(), term="kim")
    assert items[0].team_id == str(tid)
    assert items[0].team_name == "TeamX"


async def test_user_without_a_team_yields_none_not_the_string_none():
    """팀 없는 사용자의 team_id 는 None 이어야 한다 — ``"None"`` 문자열이면 UI 가
    존재하지 않는 팀을 펼치려 한다."""
    rows = [(uuid.uuid4(), "k@example.com", "K", UserRole.ADMIN, None, None)]
    ctx, _ = _patched(rows)
    with ctx:
        items, _ = await _svc().search_users(AsyncMock(), term="kim")
    assert items[0].team_id is None
    assert items[0].team_name is None


# ─────────────────────────────────────────────────────────────────────────────
# 4. 컬럼-only SELECT (성능 계약)
# ─────────────────────────────────────────────────────────────────────────────


def test_search_selects_columns_not_the_orm_entity():
    """``select(User)`` 로 되돌아가면 타이핑마다 팀·VK eager-load 가 붙는다.

    주석이 아니라 코드에서 판정한다.
    """
    src = _REPO_PY.read_text(encoding="utf-8")
    start = src.index("    async def search_users(")
    body = src[start : src.index("\n    async def ", start + 10)]
    # docstring 을 제외한 실행부만 본다.
    code = body
    for q in ('"""', "'''"):
        while code.count(q) >= 2:
            a = code.index(q)
            b = code.index(q, a + 3) + 3
            code = code[:a] + code[b:]

    assert "select(User)" not in code, (
        "search_users 가 ORM 엔티티를 select 한다 — User.team/virtual_keys 가 "
        "eager-load 되어 타이핑마다 추가 쿼리가 붙는다"
    )
    for col in ("User.id", "User.email", "User.display_name", "User.role", "User.team_id"):
        assert col in code, f"{col} 을 SELECT 하지 않는다"
    assert "outerjoin(Team" in code, "팀명을 LEFT JOIN 으로 가져오지 않는다(N+1 위험)"


def test_prefix_matches_are_ordered_first():
    """"kim" 을 치면 kim@… 이 akim@… 보다 위여야 한다.

    prefix 정렬이 없으면 사전순만 남아, 정확히 원하는 사람이 20건 밖으로 밀린다.
    """
    src = _REPO_PY.read_text(encoding="utf-8")
    start = src.index("    async def search_users(")
    body = src[start : src.index("\n    async def ", start + 10)]
    assert "order_by" in body, "정렬이 없다"
    assert ".desc()" in body, "prefix 일치를 우선하는 정렬이 없다"
    assert f'{"prefix"}' in body, "prefix 패턴을 만들지 않는다"
