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

#: session.info 키 — 커밋으로 발사된 (task, surface_failure) 목록. 요청 스코프
#: 드레이너(CommittingRoute·get_db_session, core/db.py)가 응답 전에 이 목록의
#: 태스크 완료를 기다려, 지연 SET 이 응답보다 늦게 끝나거나 조용히 실패하는
#: 일을 막는다(R21 r3 S1).
DEFERRED_TASKS_INFO_KEY = "deferred_redis_write_tasks"


# best-effort 지연 쓰기에 응답을 얼마나 오래 물려둘지 — SCAN+DEL 같은 긴
# 무효화나 실패 기록용 커넥션 대기(pool_timeout)가 응답 지연/풀 고갈이 되지
# 않게 상한을 둔다(R21 r4 S2). 지나면 태스크는 계속 살아서 완료된다.
_DEFERRED_DRAIN_TIMEOUT = 2.0


async def drain_deferred_redis_writes(session: AsyncSession) -> None:
    """커밋으로 발사된 지연 Redis 쓰기 태스크의 완료를 기다린다.

    응답 직전에 호출한다 — ``key:vk:{hash}`` SET 같은 쓰기가 응답 뒤에
    완료되면 클라이언트의 첫 게이트웨이 호출이 레이스로 401 을 받고, 실패는
    TTL 동안 유지되는 dead credential 이 된다. ``surface_failure=True`` 로
    등록된 쓰기의 예외는 여기서 올라간다 — 응답 전이라면 요청이 500 으로
    바뀌어 클라이언트가 재시도할 수 있다(orphan DB 행은 무해).

    ``asyncio.wait`` 를 쓴다 — gather 는 호출자가 취소될 때 자식 태스크까지
    취소해, 커밋된 폐기의 무효화 DEL 같은 쓰기가 pod 종료/클라이언트 끊김에
    죽을 수 있다. wait 는 호출자가 취소돼도 자식을 살려둔다(_DEFERRED_TASKS
    강참조). surfaced 태스크만 기한 없이 기다리고 나머지는 짧은 상한을 둔다.
    """
    # 테스트 더블(MagicMock/AsyncMock) 세션은 info 가 dict 가 아닐 수 있다.
    info = getattr(session, "info", None)
    if not isinstance(info, dict):
        return
    tasks = info.pop(DEFERRED_TASKS_INFO_KEY, None)
    if not tasks:
        return
    surfaced = [t for t, s in tasks if s]
    best_effort = [t for t, s in tasks if not s]
    # surfaced 를 먼저 기다린다 — 실패하면 즉시 올려서 best-effort 쓰기가
    # 실패 응답까지 붙잡지 않게 한다(성공 태스크는 백그라운드에서 계속).
    # 성공하면 남은 시간 예산 안에서만 best-effort 를 기다린다(R21 r5 N4).
    deadline = asyncio.get_running_loop().time() + _DEFERRED_DRAIN_TIMEOUT
    if surfaced:
        done, _pending = await asyncio.wait(surfaced)
        for t in done:
            if t.cancelled():  # drain 자체는 안 죽였는데 태스크가 취소된 경우
                # 사실상 루프 종료 시점 — 삼키면 dead credential 을 성공으로
                # 보고할 수 있어 error 로 남긴다(R21 r5 N5).
                logger.error("deferred_redis_write_cancelled")
                continue
            exc = t.exception()
            if exc is not None:
                raise exc
    if best_effort:
        _done, pending = await asyncio.wait(
            best_effort,
            timeout=max(0.0, deadline - asyncio.get_running_loop().time()),
        )
        if pending:
            # 죽이지 않는다 — 완료 때까지 백그라운드에서 계속된다.
            logger.warning("deferred_redis_writes_slow", pending=len(pending))


async def defer_redis_write_until_commit(
    session: AsyncSession,
    write: Callable[[], Awaitable[None]],
    *,
    surface_failure: bool = False,
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

    ``surface_failure=True`` — 쓰기 실패를 조용히 삼키지 않고 요청 스코프
    드레이너(``drain_deferred_redis_writes``, 응답 전 완료 보장)까지 올린다.
    VK 인증키 SET 처럼 실패가 dead credential 을 만드는 쓰기용. 기본값
    False 는 기존 동작(best-effort, 실패는 경고 로그).

    ⚠️ ``write`` 안에서 ``session`` 을 다시 쓰지 말 것 — 지연 시점에는 이미
    커밋된 뒤라 같은 세션의 새 쿼리가 응답 경로와 동시에 실행될 수 있다(세션은
    태스크 간 공유가 안전하지 않다). DB 값이 필요하면 커밋 전에 읽어 클로저로
    넘긴다.
    """
    sync_session = getattr(session, "sync_session", None)
    if not isinstance(sync_session, Session):
        # mock 세션(단위/통합 테스트) — 즉시 실행해 기존 동작을 유지한다.
        await write()
        return

    fired = {"live": True}
    # 지연 쓰기가 등록된 트랜잭션 스코프 — savepoint 안에서 등록됐으면 그
    # savepoint(또는 더 깊은 중첩)가 롤백될 때 함께 죽어야 한다. 안 그러면
    # 롤백된 변경의 DEL/SET 이 외부 커밋 때 실행돼 DB 는 ACTIVE 인데 Redis 에선
    # 지워지는 정합 붕괴가 생긴다(예: per-user savepoint 안의 VK 폐기 실패).
    # 현재 진행 중인 가장 깊은 트랜잭션 — get_nested_transaction() 이 savepoint
    # 안이면 그 SessionTransaction 을, 아니면 루트를 잡는다(SessionTransaction
    # .nested 는 자식 링크가 아니라 flag 라 아래로 못 내려간다 — 실측 확인).
    txn_at_defer = (
        sync_session.get_nested_transaction()
        or sync_session.get_transaction()
    )

    def _is_ancestor_or_self(t, ancestor) -> bool:
        while t is not None:
            if t is ancestor:
                return True
            t = t.parent
        return False

    async def _guarded() -> None:
        try:
            await write()
        except Exception:
            # surfaced 실패도 여기서 남긴다 — drain 을 못 타는 경로(요청 외 세션,
            # 취소된 drain)에서 흔적 없이 사라지지 않게 한다(R21 r4 S3).
            logger.warning(
                "redis_deferred_write_failed", surfaced=surface_failure,
                exc_info=True,
            )
            if surface_failure:
                raise

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
            # 요청 스코프 드레이너가 응답 전에 완료를 기다리도록 세션에 기록한다.
            # 태스크의 예외를 미리 회수해두면(drain 전 GC 등) "exception was never
            # retrieved" 경고를 막는다 — drain 은 asyncio.wait 로 결과를 다시 읽는다.
            task.add_done_callback(
                lambda t: t.exception() if not t.cancelled() else None
            )
            # 테스트 더블 세션은 info 가 dict 가 아닐 수 있다 — 기록만 건너뛰고
            # 태스크 발사 자체는 유지한다(기존 fire-and-forget 동작).
            info = getattr(session, "info", None)
            if isinstance(info, dict):
                info.setdefault(DEFERRED_TASKS_INFO_KEY, []).append(
                    (task, surface_failure)
                )
        except RuntimeError:
            logger.warning("redis_deferred_write_no_loop")

    def _on_rollback(_s) -> None:
        if sync_session.in_nested_transaction():
            # SAVEPOINT 롤백 — _on_soft_rollback 이 등록 스코프 기준으로 판별한다.
            return
        fired["live"] = False

    def _on_soft_rollback(_s, previous_transaction) -> None:
        """savepoint/중첩 롤백 — 지연 쓰기의 등록 트랜잭션이 롤백된 트랜잭션의
        자신~자손이면 그 쓰기는 롤백된 DB 변경에 대한 것이므로 함께 폐기한다."""
        if txn_at_defer is None:
            return
        if _is_ancestor_or_self(txn_at_defer, previous_transaction):
            fired["live"] = False

    event.listen(sync_session, "after_commit", _on_commit)
    event.listen(sync_session, "after_rollback", _on_rollback)
    event.listen(sync_session, "after_soft_rollback", _on_soft_rollback)
