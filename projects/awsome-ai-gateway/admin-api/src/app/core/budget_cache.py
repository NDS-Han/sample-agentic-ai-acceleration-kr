# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""커밋-후-지연 Redis 쓰기 유틸리티.

``budget:config:user:{uid}`` 키의 쓰기 정책(H3): **admin-api 는 이 키를
SET 하지 않는다 — DEL 만 한다.** 그 키에는 총액 필드와 ``app_clients``
게이트가 공존하는데, 어느 쪽의 지연 SET 도 다른 커밋과 얽혀 마지막에 도착한
stale 스냅샷이 TTL 동안 이기는 경합이 있었다(트랜잭션 락은 지연 태스크 실행
순서를 보장하지 않는다). 게이트웨이는 미스 시 ``ensure_config_cached`` 로
DB 에서 완전한 설정을 원자 재도출하므로 DEL 만이 교환 가능하고 안전하다.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

import structlog
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

logger = structlog.get_logger()

#: 지연 Redis 쓰기 태스크의 강참조 — ``asyncio.create_task`` 는 루프가 약참조만
#: 유지해, 보관하지 않으면 실행 전 GC 될 수 있다(문서화된 asyncio 함정).
_DEFERRED_TASKS: set[asyncio.Task] = set()

async def defer_redis_write_until_commit(
    session: AsyncSession,
    write: Callable[[], Awaitable[None]],
) -> None:
    """``session`` 이 커밋될 때까지 Redis **쓰기(SET)** 를 지연한다.

    왜 필요한가 — 예산 서비스의 캐시 갱신에는 두 부류가 있다:

    * **DEL** — 커밋 전 실행은 **rollback 케이스에서만** 자가치유된다(캐시 미스로
      남아 게이트웨이가 커밋된 DB 행으로 재수화). 그러나 **commit-성공 경로의
      레이스**는 다르다: DEL→commit 창에 들어온 요청이 miss 로 **미커밋 구 행**을
      읽어 ≤TTL 동안 재캐시한다(R4-A2-3). 그래서 신규 코드는 DEL 도
      ``invalidate_after_commit``(core/cache_invalidation.py)으로 지연한다.
    * **SET** — 커밋 전에 실행하면, 그 뒤 롤백 시 DB 에 존재하지 않는 예산
      설정을 TTL 동안 광고한다(§6-6 위반 — 미커밋 T/D 로 실제 요청이 차단될 수
      있다).

    그래서 SET 만 ``after_commit`` 까지 민다 — 커밋 성공 시에만 실행되고,
    롤백되면 폐기된다. 세션이 이벤트를 지원하지 않으면(단위/통합 테스트의
    AsyncMock) 기존 동작과 같이 즉시 실행한다.

    ⚠️ ``write`` 안에서 ``session`` 을 다시 쓰지 말 것 — 지연 시점에는 이미
    커밋된 뒤라 같은 세션의 새 쿼리가 응답 경로와 동시에 실행될 수 있다(세션은
    태스크 간 공유가 안전하지 않다). DB 값이 필요하면 커밋 전에 읽어 클로저로
    넘긴다.
    """
    fired = {"live": True}

    async def _guarded() -> None:
        try:
            await write()
        except Exception:
            logger.warning("redis_deferred_write_failed", exc_info=True)

    # 리스너 해제는 안 한다 — 디스패치 도중 event.remove 를 부르면 SQLAlchemy 가
    # deque mutation RuntimeError 를 낸다. fired 플래그가 재실행을 막고, 리스너는
    # 세션과 함께 GC 된다.
    def _on_commit(_s) -> None:
        if not fired["live"]:
            return
        if sync_session.in_nested_transaction():
            # SAVEPOINT(release) 에도 after_commit 이 발화한다 — 이 이벤트가
            # nested 트랜잭션의 것이면 외부 트랜잭션은 아직 열려 있으므로 쓰기를
            # 실행하면 안 된다. 여기서 DEL/SET 이 새어나가면 outer commit 전에
            # 게이트웨이가 미커밋 상태를 읽어간다(조기실행은 R3-8 과 같은 부류).
            # in_transaction() 은 outer commit 의 after_commit 시점에도 True 라
            # 구분자로 쓸 수 없고, in_nested_transaction() 이 정확하다(실측 확인).
            return
        fired["live"] = False
        try:
            task = asyncio.get_running_loop().create_task(_guarded())
            _DEFERRED_TASKS.add(task)
            task.add_done_callback(_DEFERRED_TASKS.discard)
        except RuntimeError:
            logger.warning("redis_deferred_write_no_loop")

    def _on_rollback(_s) -> None:
        if sync_session.in_nested_transaction():
            # SAVEPOINT 롤백 — 외부 트랜잭션은 계속 진행 중이므로 지연 쓰기 유지.
            return
        fired["live"] = False

    sync_session = getattr(session, "sync_session", None)
    if not isinstance(sync_session, Session):
        # mock 세션(단위/통합 테스트) — 즉시 실행해 기존 동작을 유지한다.
        await write()
        return
    event.listen(sync_session, "after_commit", _on_commit)
    event.listen(sync_session, "after_rollback", _on_rollback)
