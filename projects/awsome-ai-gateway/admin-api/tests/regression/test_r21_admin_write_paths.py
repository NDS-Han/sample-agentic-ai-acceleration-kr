# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""R21 admin-api 관리 쓰기 경로 회귀 — 라운드 1 M/S/N 수정분의 런타임 가드.

커버:
- M2: audit 필드 클립 + 배치 실패 시 단건 폴백
- S3: service-token expiry/name 경계, rotate 계보 연쇄 폐기
- S5: 세션 바인딩 감사(트랜잭션 동거) vs queue_only
- N6: 팀 허용 모델에 비활성 별칭 거부
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError as PydanticValidationError

from app.core.audit import AuditLogger
from app.core.exceptions import ValidationError
from app.models.auth import ServiceToken
from app.schemas.service_tokens import ServiceTokenCreateRequest
from app.services.service_token_service import ServiceTokenService
from app.services.team_allowed_model_service import TeamAllowedModelService


def _audit_kwargs(**over):
    kw = dict(
        actor_user_id=uuid.uuid4(),
        actor_role="ADMIN",
        action="TEST_ACTION",
        resource_type="Test",
        resource_id="r-1",
    )
    kw.update(over)
    return kw


class TestAuditFieldClipping:
    """M2: 클라이언트 조작 x-request-id / path resource_id 가 컬럼 폭을
    넘으면 배치 INSERT 전체가 실패해 무관한 행까지 유실된다."""

    @pytest.mark.asyncio
    async def test_overlong_fields_are_clipped_to_column_width(self):
        logger = AuditLogger()
        huge = "x" * 500
        await logger.log(
            None,  # session=None → 큐 경로
            **_audit_kwargs(resource_id=huge, request_id=huge),
        )
        entry = logger._queue.get_nowait()
        assert len(entry["resource_id"]) == 256
        assert len(entry["request_id"]) == 128

    @pytest.mark.asyncio
    async def test_session_bound_write_is_also_clipped(self):
        logger = AuditLogger()
        session = MagicMock()
        session.add = MagicMock()
        await logger.log(
            session,
            **_audit_kwargs(request_id="y" * 400),
        )
        row = session.add.call_args.args[0]
        assert len(row.request_id) == 128
        # 세션 바인딩 경로는 큐에 넣지 않는다
        assert logger.queue_size == 0

    @pytest.mark.asyncio
    async def test_poisoned_row_does_not_kill_whole_batch(self):
        """벌크 INSERT 실패 시 단건 폴백 — 한 독 행이 99개 무관한 행을
        버리지 않는다."""
        logger = AuditLogger(batch_size=10)

        committed: list[list] = []
        calls = {"n": 0}

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def __init__(self, fail_bulk: bool):
                self._fail_bulk = fail_bulk

            def add(self, obj):
                self._rows = [obj]

            def add_all(self, rows):
                self._rows = list(rows)

            async def commit(self):
                calls["n"] += 1
                # 첫 번째(벌크)만 행 단위 오류로 실패 — 단건들은 성공.
                # DataError 를 쓰는 이유: 연결 오류 등 비행 오류는 폴백하지 않는다
                # (폴백은 독 행 격리용, 인프라 장애에 100회 재시도 금지).
                if len(getattr(self, "_rows", [])) > 1:
                    from sqlalchemy.exc import DataError

                    raise DataError("INSERT", {}, Exception("value too long"))
                committed.append(getattr(self, "_rows", []))

        def factory():
            return _FakeSession(True)

        logger._session_factory = factory
        batch = [
            {
                "id": uuid.uuid4(),
                "timestamp": datetime.now(timezone.utc),
                "actor_user_id": uuid.uuid4(),
                "actor_role": "ADMIN",
                "action": "A",
                "resource_type": "T",
                "resource_id": str(i),
                "changes": {},
                "result": "SUCCESS",
                "ip_address": "0.0.0.0",
                "request_id": "",
            }
            for i in range(3)
        ]
        await logger._flush_batch(batch)
        # 벌크 1회 실패 + 단건 3회 성공 — 총 3행 살아남는다
        assert sum(len(r) for r in committed) == 3

    @pytest.mark.asyncio
    async def test_infra_error_does_not_trigger_per_row_retry(self):
        """연결 오류/풀 타임아웃에 단건 폴백을 돌리면 실패당 pool_timeout 만큼
        컨슈머가 멈춘다 — 행 단위 오류(DataError/IntegrityError)만 폴백한다."""
        logger = AuditLogger(batch_size=10)

        commits = {"n": 0}

        class _FakeSession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            def add(self, obj):
                pass

            def add_all(self, rows):
                pass

            async def commit(self):
                commits["n"] += 1
                raise RuntimeError("connection lost")

        logger._session_factory = lambda: _FakeSession()
        batch = [
            {
                "id": uuid.uuid4(),
                "timestamp": datetime.now(timezone.utc),
                "actor_user_id": uuid.uuid4(),
                "actor_role": "ADMIN",
                "action": "A",
                "resource_type": "T",
                "resource_id": str(i),
                "changes": {},
                "result": "SUCCESS",
                "ip_address": "0.0.0.0",
                "request_id": "",
            }
            for i in range(5)
        ]
        await logger._flush_batch(batch)
        # 벌크 1회만 시도하고 폴백하지 않는다 — 인프라 장애 재시도 폭풍 차단
        assert commits["n"] == 1


class TestAuditSessionBinding:
    """S5: 요청 세션을 받은 성공 감사는 비즈니스 트랜잭션에 묶이고,
    queue_only=True 는 실패 경로 기록용으로 큐에 남는다."""

    @pytest.mark.asyncio
    async def test_session_bound_entry_goes_to_session_not_queue(self):
        logger = AuditLogger()
        session = MagicMock()
        session.add = MagicMock()
        await logger.log(session, **_audit_kwargs())
        session.add.assert_called_once()
        assert logger.queue_size == 0

    @pytest.mark.asyncio
    async def test_queue_only_bypasses_session(self):
        """실패 응답과 함께 롤백되는 세션에 묶으면 기록이 사라진다 —
        열람 시도 감사 같은 경로는 큐로 간다."""
        logger = AuditLogger()
        session = MagicMock()
        session.add = MagicMock()
        await logger.log(session, queue_only=True, **_audit_kwargs())
        session.add.assert_not_called()
        assert logger.queue_size == 1

    @pytest.mark.asyncio
    async def test_service_token_id_recorded_in_changes(self):
        """S4: svc- 토큰 호출자는 합성 user_id 를 공유하므로 토큰 id 를
        changes 에 남겨야 행위자를 특정할 수 있다."""
        logger = AuditLogger()
        tok_id = uuid.uuid4()
        await logger.log(
            None, service_token_id=tok_id, **_audit_kwargs()
        )
        entry = logger._queue.get_nowait()
        assert entry["changes"]["_service_token_id"] == str(tok_id)


class TestServiceTokenBounds:
    """S3: 무제한 expiry_days 는 사실상 영구 ADMIN 자격증명이고,
    경계 없는 name 은 OverflowError/비대 DB 행을 만든다."""

    def test_expiry_days_upper_bound(self):
        with pytest.raises(PydanticValidationError):
            ServiceTokenCreateRequest(name="t", expiry_days=366)
        with pytest.raises(PydanticValidationError):
            ServiceTokenCreateRequest(name="t", expiry_days=0)

    def test_name_length_bound(self):
        with pytest.raises(PydanticValidationError):
            ServiceTokenCreateRequest(name="")
        with pytest.raises(PydanticValidationError):
            ServiceTokenCreateRequest(name="n" * 129)


class TestServiceTokenRevokeCascade:
    """S3: 유출 토큰이 self-rotate 로 만든 후속 토큰까지 폐기한다."""

    @pytest.mark.asyncio
    async def test_revoke_cascades_to_rotated_successors(self):
        t1 = MagicMock(spec=ServiceToken)
        t1.id = uuid.uuid4()
        t1.revoked_at = None
        t2 = MagicMock(spec=ServiceToken)
        t2.id = uuid.uuid4()
        t2.revoked_at = None
        t3 = MagicMock(spec=ServiceToken)
        t3.id = uuid.uuid4()
        t3.revoked_at = None

        children = {t1.id: [t2], t2.id: [t3], t3.id: []}
        revoked_ids: list[uuid.UUID] = []

        with patch(
            "app.services.service_token_service.ServiceTokenRepository"
        ) as Repo:
            repo = Repo.return_value
            repo.get_by_id = AsyncMock(return_value=t1)
            repo.list_rotated_from = AsyncMock(
                side_effect=lambda tid: children[tid]
            )

            async def _set_revoked_at(tid, at):
                revoked_ids.append(tid)

            repo.set_revoked_at = AsyncMock(side_effect=_set_revoked_at)

            svc = ServiceTokenService()
            await svc.revoke(MagicMock(), token_id=t1.id)

        assert revoked_ids == [t1.id, t2.id, t3.id] or set(revoked_ids) == {
            t1.id,
            t2.id,
            t3.id,
        }

    @pytest.mark.asyncio
    async def test_revoke_cascades_past_already_dead_ancestor(self):
        """grace 만료로 이미 revoked_at<=now 인 조상을 폐기해도 자손 탐색은
        계속한다 — 스킵하면 유출 토큰이 self-rotate 한 후속 토큰이 살아남는다
        (R21 r2 M-C)."""
        now = datetime.now(timezone.utc)
        dead = MagicMock(spec=ServiceToken)
        dead.id = uuid.uuid4()
        dead.revoked_at = now - timedelta(hours=25)  # grace 만료 → 이미 사망
        child = MagicMock(spec=ServiceToken)
        child.id = uuid.uuid4()
        child.revoked_at = None  # 살아있는 후속 토큰

        children = {dead.id: [child], child.id: []}
        revoked_ids: list[uuid.UUID] = []

        with patch(
            "app.services.service_token_service.ServiceTokenRepository"
        ) as Repo:
            repo = Repo.return_value
            repo.get_by_id = AsyncMock(return_value=dead)
            repo.list_rotated_from = AsyncMock(side_effect=lambda tid: children[tid])

            async def _set(tid, at):
                revoked_ids.append(tid)

            repo.set_revoked_at = AsyncMock(side_effect=_set)

            svc = ServiceTokenService()
            await svc.revoke(MagicMock(), token_id=dead.id)

        assert revoked_ids == [child.id]  # 조상은 이미 폐기 → 자손만 쓰기

    @pytest.mark.asyncio
    async def test_revoke_overwrites_grace_to_now(self):
        """grace(revoked_at=미래) 중인 토큰도 즉시 폐기로 덮어쓴다 —
        24h grace 를 두고 노는 사이 공격자가 쓸 수 있다."""
        now = datetime.now(timezone.utc)
        tok = MagicMock(spec=ServiceToken)
        tok.id = uuid.uuid4()
        tok.revoked_at = now + timedelta(hours=20)  # grace 중

        with patch(
            "app.services.service_token_service.ServiceTokenRepository"
        ) as Repo:
            repo = Repo.return_value
            repo.get_by_id = AsyncMock(return_value=tok)
            repo.list_rotated_from = AsyncMock(return_value=[])
            repo.set_revoked_at = AsyncMock()

            svc = ServiceTokenService()
            await svc.revoke(MagicMock(), token_id=tok.id)

        repo.set_revoked_at.assert_awaited_once()
        _, at = repo.set_revoked_at.await_args.args
        assert at <= datetime.now(timezone.utc)


class TestTeamAllowedModelActiveCheck:
    """N6: 주석은 'ACTIVE 검증'이라 했는데 존재만 확인했다 — 비활성 별칭을
    화이트리스트에 넣으면 재활성화 순간 자동 허용된다."""

    @pytest.mark.asyncio
    async def test_inactive_alias_rejected(self):
        from app.models.model import ModelStatus

        svc = TeamAllowedModelService(cache_mgr=MagicMock())
        session = MagicMock()
        team_id = uuid.uuid4()
        actor = MagicMock()
        actor.user_id = uuid.uuid4()
        actor.role.value = "ADMIN"

        team = MagicMock()
        inactive_model = MagicMock()
        inactive_model.status = ModelStatus.INACTIVE

        with patch(
            "app.services.team_allowed_model_service.UserRepository"
        ) as URepo, patch(
            "app.services.team_allowed_model_service.ModelRepository"
        ) as MRepo, patch(
            "app.services.team_allowed_model_service.TeamAllowedModelRepository"
        ) as TRepo:
            URepo.return_value.get_team = AsyncMock(return_value=team)
            TRepo.return_value.list_by_team = AsyncMock(return_value=[])
            MRepo.return_value.get_by_alias = AsyncMock(
                return_value=inactive_model
            )
            with pytest.raises(ValidationError, match="not ACTIVE"):
                await svc.set_for_team(
                    session,
                    team_id=team_id,
                    model_aliases=["dead-alias"],
                    actor=actor,
                )

    @pytest.mark.asyncio
    async def test_already_listed_inactive_alias_allowed(self):
        """r3 N4: 등록 후 비활성화된 별칭을 풀-리플레이스 PUT 이 다시내도
        거부하지 않는다 — 안 그러면 운영자가 그 항목을 지울 때까지 목록 편집
        자체가 막힌다."""
        from app.models.model import ModelStatus

        svc = TeamAllowedModelService(cache_mgr=MagicMock())
        session = MagicMock()
        team_id = uuid.uuid4()
        actor = MagicMock()
        actor.user_id = uuid.uuid4()
        actor.role.value = "ADMIN"

        team = MagicMock()
        dead = MagicMock()
        dead.status = ModelStatus.INACTIVE

        with patch(
            "app.services.team_allowed_model_service.UserRepository"
        ) as URepo, patch(
            "app.services.team_allowed_model_service.ModelRepository"
        ) as MRepo, patch(
            "app.services.team_allowed_model_service.TeamAllowedModelRepository"
        ) as TRepo:
            URepo.return_value.get_team = AsyncMock(return_value=team)
            TRepo.return_value.list_by_team = AsyncMock(
                return_value=["dead-alias"]
            )
            TRepo.return_value.set_for_team = AsyncMock(
                return_value=["dead-alias"]
            )
            # 이미 목록에 있는 별칭은 get_by_alias 조회조차 하지 않는다
            MRepo.return_value.get_by_alias = AsyncMock(side_effect=AssertionError("should not be called"))
            resp = await svc.set_for_team(
                session,
                team_id=team_id,
                model_aliases=["dead-alias"],
                actor=actor,
            )
        assert resp.model_aliases == ["dead-alias"]
