# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""조직 트리의 카운트 필드가 노드 타입에 따라 다른 것을 뜻하지 않는지.

배경 — ``OrgNodeMeta.member_count`` 한 필드가 두 의미로 쓰이고 있었다:

  * TEAM       → 활성 사용자 수
  * DEPARTMENT → 하위 팀들의 활성 사용자 **합**
  * ORGANIZATION → 전체 멤버 수(**비활성 포함**)

그리고 admin-ui 의 부서 상세는 그 값을 **팀 수**로 읽어 표시했다
(``OrgDetailPanel.tsx``). 그래서 20팀 × 50명 부서가 화면에 "팀 1000개" 로 떴다.
필드 하나가 노드 타입마다 다른 의미를 가지면 그 오독은 언젠가 반드시 일어난다.

두 번째, 독립된 결함: 조직 노드는 ORM 의 ``t.members`` 전체를 셌고 하위 노드는
``active_members`` 만 셌다. 그래서 **조직 합계 ≠ 부서 합계의 총합**이었고, 화면만
보고는 어느 쪽이 맞는지 판단할 수 없었다.

이 파일이 고정하는 것:
  1. ``member_count`` 는 **항상 사람 수**다(노드 타입 무관).
  2. ``team_count`` 는 DEPARTMENT / ORGANIZATION 에서만 채워진다.
  3. 계층 합이 일치한다 — 조직 = Σ부서, 부서 = Σ팀.
  4. 비활성 사용자는 **어느 레벨에서도** 세지 않는다.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.user_team_service import UserTeamService


def _member(*, active: bool = True, name: str = "u", role: str = "DEVELOPER"):
    m = MagicMock()
    m.id = uuid.uuid4()
    m.display_name = name
    m.email = f"{name}@example.com"
    m.is_active = active
    m.role = MagicMock()
    m.role.value = role
    return m


def _team(*, name: str, members: list, leader=None):
    t = MagicMock()
    t.id = uuid.uuid4()
    t.name = name
    t.members = members
    t.leader_user_id = leader.id if leader else None
    return t


def _dept(*, name: str, teams: list):
    d = MagicMock()
    d.id = uuid.uuid4()
    d.name = name
    d.teams = teams
    return d


def _org(*, name: str, departments: list):
    o = MagicMock()
    o.id = uuid.uuid4()
    o.name = name
    o.departments = departments
    return o


async def _build(org, *, include_empty: bool = False, custom_user_ids=()):
    """``get_org_tree`` 를 태워 트리 노드를 얻는다. 리포지토리만 대체한다.

    실제 시그니처는 ``UserTeamService(cache_mgr=..., key_service=...)`` 이고 트리는
    ``UserRepository.list_all_orgs()`` 에서 온다 — 그 둘만 대체하고 노드 조립 로직은
    실물을 그대로 태운다(카운트 계산이 검증 대상이므로).

    ``custom_user_ids`` — 개별정책 DISTINCT union 쿼리(session.execute)가
    돌려줄 user_id 목록. user_allowed_* 테이블이 비어 있는 기본값은 빈 목록.
    """
    from app.core.cache_invalidation import CacheInvalidationManager
    from app.services.key_service import KeyService

    cache_mgr = MagicMock(spec=CacheInvalidationManager)
    cache_mgr._redis = MagicMock()
    svc = UserTeamService(cache_mgr=cache_mgr, key_service=MagicMock(spec=KeyService))
    session = AsyncMock()
    union_result = MagicMock()
    union_result.scalars.return_value = list(custom_user_ids)
    session.execute = AsyncMock(return_value=union_result)
    with patch("app.services.user_team_service.UserRepository") as MockRepo:
        MockRepo.return_value.list_all_orgs = AsyncMock(return_value=[org])
        return await svc.get_org_tree(session, include_empty=include_empty)


# ─────────────────────────────────────────────────────────────────────────────
# 픽스처 — 20팀 × 50명 부서(원래 버그가 드러난 정확한 형상) + 비활성 섞기
# ─────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def tree_org():
    """부서 A: 20팀 × (활성 50 + 비활성 3), 부서 B: 2팀 × 활성 1."""
    dept_a_teams = [
        _team(
            name=f"A-team-{i}",
            members=[_member(name=f"a{i}-{j}") for j in range(50)]
            + [_member(name=f"a{i}-x{j}", active=False) for j in range(3)],
        )
        for i in range(20)
    ]
    dept_b_teams = [_team(name=f"B-team-{i}", members=[_member(name=f"b{i}")]) for i in range(2)]
    return _org(
        name="Org",
        departments=[_dept(name="A", teams=dept_a_teams), _dept(name="B", teams=dept_b_teams)],
    )


@pytest.fixture
async def built(tree_org):
    return await _build(tree_org)


def _by_type(node, kind):
    out = []
    stack = [node]
    while stack:
        n = stack.pop()
        if n.type == kind:
            out.append(n)
        stack.extend(n.children or [])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# 1. 부서: 팀 수와 사람 수가 서로 다른 필드로 나온다
# ─────────────────────────────────────────────────────────────────────────────


async def test_department_team_count_is_teams_not_people(built):
    """이게 원래 버그다 — 20팀 부서가 team_count 20 을 줘야 한다(1000 이 아니라)."""
    depts = {d.name: d for d in _by_type(built, "DEPARTMENT")}
    a = depts["A"]
    assert a.meta.team_count == 20, (
        f"부서 A 의 team_count 가 {a.meta.team_count} — 사람 수(1000)가 새어 들어왔다"
    )
    assert a.meta.member_count == 1000, (
        f"부서 A 의 member_count 가 {a.meta.member_count} — 활성 50×20 이어야 한다"
    )
    # 두 값이 **다르다**는 것 자체가 필드 분리가 실제로 일어났다는 증거다.
    assert a.meta.team_count != a.meta.member_count


async def test_department_excludes_inactive_members(built):
    """부서 A 에는 팀마다 비활성 3명이 있다 — 세면 1060 이 된다."""
    depts = {d.name: d for d in _by_type(built, "DEPARTMENT")}
    assert depts["A"].meta.member_count == 1000, "비활성 사용자가 부서 합계에 섞였다"


# ─────────────────────────────────────────────────────────────────────────────
# 2. 조직: 하위와 같은 규칙으로 센다
# ─────────────────────────────────────────────────────────────────────────────


async def test_organization_counts_match_the_sum_of_departments(built):
    """예전엔 조직만 비활성을 포함해 세서 계층 합이 어긋났다."""
    depts = _by_type(built, "DEPARTMENT")
    assert built.type == "ORGANIZATION"
    assert built.meta.member_count == sum(d.meta.member_count for d in depts), (
        f"조직 {built.meta.member_count} ≠ Σ부서 {sum(d.meta.member_count for d in depts)}"
    )
    assert built.meta.team_count == sum(d.meta.team_count for d in depts)


async def test_organization_member_count_excludes_inactive(built):
    """1000(A) + 2(B) = 1002. 비활성을 포함하면 1062 가 된다."""
    assert built.meta.member_count == 1002, (
        f"조직 member_count 가 {built.meta.member_count} — 비활성 60명이 섞였다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. 팀 / 사용자: team_count 는 의미가 없으므로 None
# ─────────────────────────────────────────────────────────────────────────────


async def test_team_nodes_have_no_team_count(built):
    """TEAM 은 팀이 아니라 사람을 담는다 — 0 이 아니라 None 이어야 한다.

    0 을 주면 UI 가 "팀 0개" 를 렌더할 수 있고, 그건 "해당 없음" 과 다른 주장이다.
    """
    teams = _by_type(built, "TEAM")
    assert teams, "TEAM 노드가 없다 — 이 테스트의 전제가 깨졌다"
    assert all(t.meta.team_count is None for t in teams), (
        "TEAM 노드에 team_count 가 채워져 있다"
    )


async def test_team_member_count_is_active_only(built):
    teams = {t.name: t for t in _by_type(built, "TEAM")}
    assert teams["A-team-0"].meta.member_count == 50, "팀 합계에 비활성이 섞였다"


async def test_user_nodes_have_neither_count(built):
    users = _by_type(built, "USER")
    assert users, "USER 노드가 없다"
    assert all(u.meta.member_count is None for u in users)
    assert all(u.meta.team_count is None for u in users)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 계약 — 프론트엔드가 읽는 필드와 일치하는가
# ─────────────────────────────────────────────────────────────────────────────


def test_frontend_type_declares_both_fields():
    """admin-ui 의 OrgNodeMeta 타입에 두 필드가 다 있어야 한다.

    ⚠️ 서버가 새 필드를 주는데 TS 인터페이스에 없으면 `node.meta.team_count` 가
       컴파일 오류이거나(strict) 조용히 undefined 다. 그러면 UI 는 폴백으로 떨어져
       고친 버그가 그대로 보인다 — 백엔드만 고치고 끝낼 수 없는 이유다.
    """
    from pathlib import Path

    ts = (
        Path(__file__).resolve().parents[3] / "admin-ui" / "src" / "types" / "entities.ts"
    ).read_text(encoding="utf-8")
    # 대조군 — 파일을 실제로 찾았는가.
    assert "OrgNodeMeta" in ts, "entities.ts 에서 OrgNodeMeta 를 찾지 못했다"
    block = ts[ts.index("export interface OrgNodeMeta") :]
    block = block[: block.index("}")]
    assert "team_count" in block, "admin-ui OrgNodeMeta 에 team_count 가 없다"
    assert "member_count" in block


def test_department_panel_reads_team_count_not_member_count():
    """UI 가 부서의 팀 수를 member_count 에서 읽으면 원래 버그가 재발한다.

    주석은 증거가 아니므로 주석을 걷어낸 코드에서 판정한다.
    """
    import re
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[3]
        / "admin-ui"
        / "src"
        / "components"
        / "users"
        / "OrgDetailPanel.tsx"
    ).read_text(encoding="utf-8")
    code = re.sub(r"/\*[\s\S]*?\*/", "", src)
    code = "\n".join(ln for ln in code.splitlines() if not ln.strip().startswith("//"))

    # 대조군 — 주석 제거가 no-op 이 아니다(원본에는 설명 주석이 있다).
    assert len(code) < len(src), "주석 제거가 아무것도 지우지 않았다"

    m = re.search(r"const\s+teamCount\s*=\s*([^;]+);", code)
    assert m, "teamCount 대입을 찾지 못했다 — UI 구조가 바뀌었다"
    expr = m.group(1)
    assert "team_count" in expr, f"teamCount 를 team_count 에서 읽지 않는다: {expr}"
    assert "member_count" not in expr, (
        f"teamCount 폴백에 member_count 가 남아 있다 — 팀 수 자리에 사람 수가 뜬다: {expr}"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 5. include_empty — 멤버 0인 팀의 정책 진입점 (/users "빈 팀 표시" 토글)
# ─────────────────────────────────────────────────────────────────────────────
#
# /models 의 팀 모델 편집기가 /users 로 이전되면서, 서버가 생략하는 빈 팀은
# 정책을 설정할 진입점이 사라진다. include_empty=true 일 때 빈 팀을 포함해야
# 하고, 빈 팀만 있는 부서도 그 팀을 담기 위해 포함돼야 한다 — 그렇지 않으면
# 빈 팀은 트리에서 영원히 도달 불가다.


@pytest.fixture
def org_with_empty_teams():
    """부서 C: 활성 팀 + 빈 팀 + 전원 비활성 팀. 부서 D: 빈 팀만. 부서 E: 팀 없음."""
    dept_c = _dept(
        name="C",
        teams=[
            _team(name="C-active", members=[_member(name="c1")]),
            _team(name="C-empty", members=[]),
            _team(name="C-inactive", members=[_member(name="cx", active=False)]),
        ],
    )
    dept_d = _dept(name="D", teams=[_team(name="D-empty", members=[])])
    dept_e = _dept(name="E", teams=[])
    return _org(name="Org", departments=[dept_c, dept_d, dept_e])


async def test_empty_teams_excluded_by_default(org_with_empty_teams):
    """기본 동작은 기존과 같다 — 빈 팀도, 빈 팀만의 부서도, 팀 없는 부서도 없다."""
    built = await _build(org_with_empty_teams)
    teams = {t.name for t in _by_type(built, "TEAM")}
    depts = {d.name for d in _by_type(built, "DEPARTMENT")}
    assert teams == {"C-active"}
    assert depts == {"C"}


async def test_include_empty_keeps_empty_teams(org_with_empty_teams):
    """include_empty=true — 활성 멤버 0인 팀(멤버 없음·전원 비활성)이 보인다."""
    built = await _build(org_with_empty_teams, include_empty=True)
    teams = {t.name: t for t in _by_type(built, "TEAM")}
    assert {"C-active", "C-empty", "C-inactive", "D-empty"} <= set(teams)
    assert teams["C-empty"].meta.member_count == 0


async def test_include_empty_keeps_dept_with_only_empty_teams(org_with_empty_teams):
    """빈 팀을 담은 부서는 포함돼야 한다 — 아니면 그 팀에 UI 로 도달 불가."""
    built = await _build(org_with_empty_teams, include_empty=True)
    depts = {d.name: d for d in _by_type(built, "DEPARTMENT")}
    assert "D" in depts, "빈 팀만 있는 부서가 빠져 D-empty 에 도달할 수 없다"
    assert "C" in depts


async def test_include_empty_still_drops_dept_with_no_teams(org_with_empty_teams):
    """팀이 하나도 없는 부서는 include_empty 에서도 제외 — 노이즈 방지."""
    built = await _build(org_with_empty_teams, include_empty=True)
    depts = {d.name for d in _by_type(built, "DEPARTMENT")}
    assert "E" not in depts


# ─────────────────────────────────────────────────────────────────────────────
# 6. has_custom_policies / custom_policy_count — 트리의 "개별 설정" 표시
# ─────────────────────────────────────────────────────────────────────────────
# /users 트리는 user_allowed_clients ∪ user_allowed_models 에 행이 있는
# 사용자에게 점을 표시하고, 팀에는 "개별 N" 카운트를 단다. 카운트는 **활성**
# 멤버만 세야 한다 — 비활성 사용자는 트리 노드가 없으므로 카운트에 포함하면
# 점 개수와 숫자가 어긋난다(섹션 1의 active-only 정합성과 같은 원칙).


@pytest.fixture
def org_with_custom_users():
    """팀 E: 활성 3명(u1=custom, u2=일반, u3=custom) + 비활성 1명(custom)."""
    u1 = _member(name="u1")
    u2 = _member(name="u2")
    u3 = _member(name="u3")
    inactive_custom = _member(name="ghost", active=False)
    team = _team(name="E", members=[u1, u2, u3, inactive_custom])
    org = _org(name="Org", departments=[_dept(name="D", teams=[team])])
    # 비활성 사용자의 id 도 DISTINCT 결과에 포함 — 카운트에서 빠져야 함을 검증.
    return org, {u1.id, u3.id, inactive_custom.id}, {u1.id, u3.id}


async def test_user_node_marks_custom_policies(org_with_custom_users):
    """user_allowed_* 에 행이 있는 활성 사용자만 has_custom_policies=true."""
    org, all_ids, active_custom = org_with_custom_users
    built = await _build(org, custom_user_ids=all_ids)
    users = {u.name: u for u in _by_type(built, "USER")}
    assert users["u1"].meta.has_custom_policies is True
    assert users["u3"].meta.has_custom_policies is True
    assert users["u2"].meta.has_custom_policies is False
    assert "ghost" not in users, "비활성 사용자는 트리 노드 자체가 없다"


async def test_team_custom_policy_count_is_active_only(org_with_custom_users):
    """TEAM 의 custom_policy_count 는 활성 멤버 교집합 — 비활성 제외."""
    org, all_ids, _ = org_with_custom_users
    built = await _build(org, custom_user_ids=all_ids)
    team = _by_type(built, "TEAM")[0]
    assert team.meta.custom_policy_count == 2  # u1 + u3 (ghost 제외)


async def test_no_custom_policies_means_zero_count(org_with_custom_users):
    """아무 개별정책도 없으면 count=0 — UI 는 배지를 숨긴다."""
    org, _, _ = org_with_custom_users
    built = await _build(org, custom_user_ids=[])
    team = _by_type(built, "TEAM")[0]
    assert team.meta.custom_policy_count == 0
    for u in _by_type(built, "USER"):
        assert u.meta.has_custom_policies is False
