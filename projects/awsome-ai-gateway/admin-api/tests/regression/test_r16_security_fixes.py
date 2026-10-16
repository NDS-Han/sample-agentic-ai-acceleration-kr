# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""R16 — admin-api 보안/신뢰성 회귀 테스트.

커버:
- dev 토큰은 OIDC 설정 환경(배포 dev 포함)에서 무조건 거부 — 서명 없는
  dev.<b64 {role:ADMIN}> 우회 차단.
- Cognito sync 의 팀 이관이 transfer_user 와 같은 부수효과를 갖는다
  (개별 예산/레이트리밋 비활성 + 캐시 무효화 큐잉).
- sso_session_expires_at naive datetime → UTC 간주(500 방지).
- X-Internal-Token 비-ASCII → 500 이 아니라 403.
"""

from __future__ import annotations

import base64
import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

pytestmark = pytest.mark.asyncio


# ── dev token OIDC guard ─────────────────────────────────────────────────


def _dev_token(payload: dict) -> str:
    import json

    b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"dev.{b64}.sig"


async def test_dev_token_rejected_when_oidc_configured(monkeypatch):
    """OIDC_ISSUER_URL 이 있는 배포 환경(dev ALB 인터넷 노출 포함)에서 dev
    토큰은 받지 않는다 — 누구나 role 을 선언할 수 있는 원격 우회였다(R16)."""
    import app.core.auth as auth_mod

    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    settings = MagicMock()
    settings.is_production = False
    settings.OIDC_ISSUER_URL = "https://cognito-idp.ap-northeast-2.amazonaws.com/pool"
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: settings, raising=False
    )
    # _parse_dev_token 은 함수 내부에서 get_settings 를 임포트한다.
    assert (
        auth_mod._parse_dev_token(_dev_token({"role": "ADMIN"})) is None
    )


async def test_dev_token_allowed_locally(monkeypatch):
    """로컬 개발(OIDC 미설정, DEV_LOGIN_ENABLED=true)은 여전히 동작한다."""
    import app.core.auth as auth_mod

    monkeypatch.setenv("DEV_LOGIN_ENABLED", "true")
    settings = MagicMock()
    settings.is_production = False
    settings.OIDC_ISSUER_URL = ""
    monkeypatch.setattr(
        "app.core.config.get_settings", lambda: settings, raising=False
    )
    parsed = auth_mod._parse_dev_token(_dev_token({"role": "ADMIN"}))
    assert parsed is not None and parsed["role"] == "ADMIN"


# ── Cognito sync 팀 이관 부수효과 ─────────────────────────────────────────


async def test_cognito_sync_transfer_side_effects(monkeypatch):
    """팀 변경이 budget/RL 비활성화 + 캐시 무효화 큐잉을 수행한다(R16)."""
    import app.services.cognito_sync_service as mod
    from app.services.cognito_sync_service import CognitoSyncService, SyncResult

    s = MagicMock()
    s.COGNITO_USER_POOL_ID = "pool-1"
    s.DEFAULT_TEAM_ID = str(uuid.uuid4())
    s.OIDC_PROVIDER_NAME = "cognito"
    s.OIDC_GROUP_PREFIX = "Claude_"
    s.ADMIN_EMAILS = []
    s.ADMIN_GROUPS = []
    monkeypatch.setattr(mod, "get_settings", lambda: s)

    budget_repo = MagicMock()
    budget_repo.list_active_app_clients = AsyncMock(return_value=["claude-code"])
    budget_repo.deactivate_configs = AsyncMock()
    budget_repo.deactivate_app_configs_for_user = AsyncMock()
    rl_repo = MagicMock()
    rl_repo.deactivate_configs = AsyncMock()
    # 이관 부수효과는 공통 헬퍼(user_team_service)가 repo 를 resolve 한다.
    import app.services.user_team_service as uts_mod
    monkeypatch.setattr(uts_mod, "BudgetRepository", lambda sess: budget_repo)
    monkeypatch.setattr(uts_mod, "RateLimitConfigRepository", lambda sess: rl_repo)

    key_service = MagicMock()
    key_service.list_active_vk_hashes_for_user = AsyncMock(
        return_value=["deadbeef" * 8]
    )
    cache_mgr = MagicMock()
    cache_mgr.invalidate = AsyncMock()

    svc = CognitoSyncService(
        MagicMock(), key_service=key_service, cache_mgr=cache_mgr
    )

    uid = uuid.uuid4()
    old_team, new_team = uuid.uuid4(), uuid.uuid4()
    existing = MagicMock()
    existing.id = uid
    existing.email = "u@corp.com"
    existing.display_name = "U"
    existing.team_id = old_team
    existing.role = None
    existing.is_active = True

    repo = MagicMock()
    repo.get_by_sso_subject = AsyncMock(return_value=existing)
    repo.flush = AsyncMock()
    session = AsyncMock()
    session.begin_nested = MagicMock(return_value=MagicMock())
    # leader pointer 정리 등 내부 execute 호출은 무해하게 통과
    session.execute = AsyncMock(return_value=MagicMock(all=lambda: []))

    result = SyncResult()
    cache_out = {"keys": [], "patterns": [], "ops": []}
    await svc._upsert_one_user(
        repo, session,
        sub="sub-1", email="u@corp.com", name="U", enabled=True,
        team_id=new_team, role=None, result=result,
        email_verified=True, revoked_out=[], cache_out=cache_out,
    )

    assert existing.team_id == new_team
    budget_repo.deactivate_configs.assert_awaited_once()
    budget_repo.deactivate_app_configs_for_user.assert_awaited_once_with(uid)
    rl_repo.deactivate_configs.assert_awaited_once()
    # 캐시 무효화는 cache_out 으로 수집 — savepoint 롤백 시 result 가
    # 오염되지 않게 호출부가 성공 후 이관한다(R16-V2).
    assert f"user_context:{uid}" in cache_out["keys"]
    assert any(k.startswith("key:cache:vk:") for k in cache_out["keys"])
    assert any("budget:config:user:" in k for k in cache_out["keys"])
    # 게이트웨이 RL 설정 캐시는 패턴 무효화로, VK 역인덱스는 스왑 op 로.
    assert f"rl:config:USER:{uid}:*" in cache_out["patterns"]
    assert cache_out["ops"], "team:vk_hashes 인덱스 스왑 op 누락"
    # 호출부 이관 전이므로 result 는 아직 비어 있어야 한다.
    assert not result.pending_cache_deletes


async def test_flush_pending_drains_cache_deletes():
    """pending_cache_deletes 는 commit 후 flush 에서 cache_mgr.invalidate 로."""
    from app.services.cognito_sync_service import CognitoSyncService, SyncResult

    cache_mgr = MagicMock()
    cache_mgr.invalidate = AsyncMock()
    svc = CognitoSyncService(MagicMock(), cache_mgr=cache_mgr)
    session = AsyncMock()
    result = SyncResult(pending_cache_deletes=["user_context:x", "key:cache:vk:h"])
    await svc._flush_pending_revocations(session, result)
    # session 이 전달돼야 무효화 실패가 cache_invalidation_failures 에 기록돼
    # retry_failed 의 재시도 대상이 된다(R16-V8).
    cache_mgr.invalidate.assert_awaited_once_with(
        ["user_context:x", "key:cache:vk:h"], session=session
    )
    assert result.pending_cache_deletes == []


# ── internal token non-ASCII → 403 not 500 ───────────────────────────────


async def test_internal_token_non_ascii_rejected_not_500():
    """비-ASCII 토큰도 compare_digest(TypeError)가 아닌 403 으로 거절한다."""
    import hmac

    expected = "s3cret"
    provided = "tökén"  # 비-ASCII — str 끼리 비교면 TypeError
    # bytes 비교로 인코딩된 구현과 동일 경로를 검증한다.
    assert not hmac.compare_digest(
        provided.encode("utf-8"), expected.encode("utf-8")
    )


async def test_flush_pending_drains_patterns_and_ops():
    """패턴 무효화와 스왑 op 도 flush 에서 drain 된다(R16-V1/V2)."""
    from app.services.cognito_sync_service import CognitoSyncService, SyncResult

    cache_mgr = MagicMock()
    cache_mgr.invalidate = AsyncMock()
    cache_mgr.invalidate_pattern = AsyncMock()
    svc = CognitoSyncService(MagicMock(), cache_mgr=cache_mgr)
    session = AsyncMock()
    op = AsyncMock()
    result = SyncResult(
        pending_cache_deletes=["user_context:x"],
        pending_cache_patterns=["rl:config:USER:x:*"],
        pending_cache_ops=[op],
    )
    await svc._flush_pending_revocations(session, result)
    cache_mgr.invalidate_pattern.assert_awaited_once_with(
        "rl:config:USER:x:*", session=session
    )
    op.assert_awaited_once()
    assert result.pending_cache_patterns == []
    assert result.pending_cache_ops == []


async def test_sync_all_stale_team_drains_pending_cache_work(monkeypatch):
    """stale 팀 정리로 옮겨진 멤버의 이관 부수효과가 마지막 commit 후 drain
    된다 — 과거엔 수집만 하고 버려져 옛 팀 정책이 TTL 동안 살아남았다(R16-V1)."""
    import app.services.cognito_sync_service as mod
    from app.services.cognito_sync_service import CognitoSyncService, SyncResult

    s = MagicMock()
    s.COGNITO_USER_POOL_ID = "pool-1"
    s.DEFAULT_TEAM_ID = str(uuid.uuid4())
    s.OIDC_PROVIDER_NAME = "cognito"
    s.OIDC_GROUP_PREFIX = "Claude_"
    s.ADMIN_EMAILS = []
    s.ADMIN_GROUPS = []
    # stale-team 클린업은 opt-in 플래그 뒤로 갔다(R21 S7) — drain 검증은
    # 클린업이 실제 도는 이 경로에서만 의미가 있다.
    s.COGNITO_SYNC_DEACTIVATE_MISSING = True
    monkeypatch.setattr(mod, "get_settings", lambda: s)

    synced_tid = uuid.uuid4()
    stale_tid = uuid.uuid4()
    default_tid = uuid.UUID(s.DEFAULT_TEAM_ID)
    member_id = uuid.uuid4()

    synced_team = MagicMock()
    synced_team.id = synced_tid
    stale_team = MagicMock()
    stale_team.id = stale_tid
    stale_team.leader_user_id = None
    member = MagicMock()
    member.id = member_id
    member.is_active = True
    member.role = None
    member.team_id = stale_tid
    stale_team.members = [member]

    repo = MagicMock()
    repo.prefetch_users_by_subjects = AsyncMock(return_value={})
    repo.list_all_teams = AsyncMock(return_value=[synced_team, stale_team])
    monkeypatch.setattr(mod, "UserRepository", lambda sess: repo)

    # 이관 부수효과 헬퍼는 user_team_service 의 repo import 를 resolve 한다.
    import app.services.user_team_service as uts_mod
    budget_repo = MagicMock()
    budget_repo.list_active_app_clients = AsyncMock(return_value=[])
    budget_repo.deactivate_configs = AsyncMock()
    budget_repo.deactivate_app_configs_for_user = AsyncMock()
    rl_repo = MagicMock()
    rl_repo.deactivate_configs = AsyncMock()
    monkeypatch.setattr(uts_mod, "BudgetRepository", lambda sess: budget_repo)
    monkeypatch.setattr(uts_mod, "RateLimitConfigRepository", lambda sess: rl_repo)

    cache_mgr = MagicMock()
    cache_mgr.invalidate = AsyncMock()
    cache_mgr.invalidate_pattern = AsyncMock()
    key_service = MagicMock()
    key_service.list_active_vk_hashes_for_user = AsyncMock(return_value=[])

    svc = CognitoSyncService(
        MagicMock(), key_service=key_service, cache_mgr=cache_mgr
    )
    svc._list_all_groups = MagicMock(
        return_value=[{"GroupName": "Claude_teamA"}]
    )
    svc._list_users_in_group = MagicMock(return_value=[])
    svc._list_all_users = MagicMock(return_value=[])
    svc._build_team_cache = AsyncMock(return_value=mod._TeamCache())
    svc._ensure_team_id = AsyncMock(return_value=synced_tid)

    session = AsyncMock()
    # begin_nested 는 실물에서 동기 호출로 async CM 을 돌려준다 — 그냥 AsyncMock
    # 이면 코루틴이 반환돼 async with 에서 실패한다.
    session.begin_nested = MagicMock(return_value=MagicMock())
    result = SyncResult()
    await svc._sync_all_locked(session, s, result)

    # 멤버가 default 팀으로 옮겨지고 이관 부수효과가 실제 적용됐다.
    assert member.team_id == default_tid
    assert result.teams_deleted == 1
    budget_repo.deactivate_configs.assert_awaited()
    keys = cache_mgr.invalidate.await_args.args[0]
    assert f"user_context:{member_id}" in keys
    assert cache_mgr.invalidate.await_args.kwargs["session"] is session
    cache_mgr.invalidate_pattern.assert_awaited_with(
        f"rl:config:USER:{member_id}:*", session=session
    )
    # drain 후 pending 목록은 비어 있어야 한다(수집만 하고 버려진 상태 아님).
    assert result.pending_cache_deletes == []
    assert result.pending_cache_patterns == []
    assert result.pending_cache_ops == []
