# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from collections.abc import AsyncGenerator, Callable

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.routing import APIRoute, request_response
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import get_settings

logger = structlog.get_logger()

# 요청 단위 세션을 매달아 두는 request.state 속성명. get_db_session 이 넣고
# CommittingRoute 가 읽는다 — 커밋 시점을 응답 생성 **전**으로 옮기기 위한 유일한 연결점.
SESSION_STATE_ATTR = "db_session"

# session.info 키 — 커밋 성공 후에야 Redis 로 나가야 하는 이벤트 대기열.
# 서비스가 트랜잭션 안에서 publish 하면 커밋 실패(롤백) 시 "취소된 쓰기의
# 알림"이 나간다(key_revoked 메일이 대표적). (channel, payload_json) 튜플 목록.
PENDING_REDIS_KEY = "pending_redis_publishes"


def enqueue_redis_publish(session: AsyncSession, channel: str, payload: str) -> None:
    """커밋 성공 시 발행할 이벤트를 세션 대기열에 넣는다.

    ⚠️ drain 은 요청 스코프(CommittingRoute · get_db_session)에만 있다 —
       요청 밖의 세션(스케줄러·직접 ``AsyncSessionLocal()``)이 커밋하면 대기열은
       아무 데도 가지 않고 세션과 함께 조용히 버려진다. 그런 호출자는 커밋 후
       ``drain_pending_redis(session, redis=...)`` 를 직접 불러야 한다.
    """
    session.info.setdefault(PENDING_REDIS_KEY, []).append((channel, payload))


async def drain_pending_redis(
    session: AsyncSession, request: Request | None = None, *, redis=None
) -> None:
    """커밋 **성공 직후** 대기열의 Redis publish 를 실행한다. 발송은 최선노력.

    ``redis`` 를 넘기면 request 없이도 쓸 수 있다 — 스케줄러 등 요청 밖 호출자용.
    """
    # 테스트 더블(MagicMock/AsyncMock) 세션은 info 가 dict 가 아닐 수 있다.
    if not isinstance(getattr(session, "info", None), dict):
        return
    # 지연 Redis 쓰기 태스크를 먼저 비운다 — publish 대기열이 비어 있거나
    # redis 가 없으면 조기 반환하므로, 먼저 하지 않으면 비-요청 세션
    # (스케줄러·sync 워커)의 태스크 기록이 session.info 에 계속 쌓인다
    # (R21 r4 N3, r5 N1). 요청 경로(CommittingRoute)에서는 이미 비어 있다.
    from app.core.budget_cache import drain_deferred_redis_writes

    try:
        await drain_deferred_redis_writes(session)
    except Exception:
        # 응답 경로가 아닌 호출자는 실패를 올리지 않는다 — surfaced 실패는
        # _guarded 가 이미 로그했다.
        logger.warning("deferred_redis_write_drain_failed", exc_info=True)
    pending = session.info.pop(PENDING_REDIS_KEY, None)
    if not pending:
        return
    if redis is None and request is not None:
        redis = getattr(request.app.state, "redis", None)
    if redis is None:
        logger.warning("pending_redis_publish_skipped", count=len(pending), reason="no redis")
        return
    for channel, payload in pending:
        try:
            await redis.publish(channel, payload)
        except Exception:
            logger.warning("pending_redis_publish_failed", channel=channel, exc_info=True)


def create_engine():
    settings = get_settings()
    return create_async_engine(
        settings.DATABASE_URL,
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        echo=settings.DB_ECHO,
        connect_args={
            "statement_cache_size": settings.DB_STATEMENT_CACHE_SIZE,
            # 블랙홀 장애(무응답)에서 asyncpg 기본값(connect 60s·명령 무제한) 대기 방지.
            "timeout": settings.DB_CONNECT_TIMEOUT,
            "command_timeout": settings.DB_COMMAND_TIMEOUT,
        },
    )


engine = create_engine()
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    """요청 단위 AsyncSession.

    ⚠️ 정상 경로의 커밋은 **여기서 하지 않는다**. yield 이후 블록은 FastAPI 가 응답을
       클라이언트로 내보낸 **뒤에** 실행하므로, 여기서 commit() 이 실패하면 그 예외는
       응답을 바꿀 수 없다. 실측(fastapi 0.136.0): 커밋이 RuntimeError 로 실패해도
       클라이언트는 `200 {"success": true, ...}` 를 받았고 등록된 예외 핸들러는 호출조차
       되지 않았다 — 즉 롤백된 쓰기를 성공으로 보고하는 조용한 데이터 손실이다.

       그래서 커밋은 CommittingRoute 가 **응답을 만든 직후, 내보내기 전에** 수행한다.
       아래 남은 커밋은 CommittingRoute 가 커밋할 수 없는 경우(스트리밍 응답)의
       마지막 안전망이다. 이미 커밋됐으면 in_transaction() 이 False 라 중복되지 않는다.
    """
    async with AsyncSessionLocal() as session:
        setattr(request.state, SESSION_STATE_ATTR, session)
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            # session.add()만으로는 트랜잭션이 열리지 않는 경우(버전/세션 구성 차이)를
            # 대비한 안전망 — pending 객체만 남은 세션도 flush 로 트랜잭션을 열어
            # 커밋 대상으로 만든다(R21 r3).
            try:
                if not session.in_transaction() and (
                    session.new or session.dirty or session.deleted
                ):
                    await session.flush()
                if session.in_transaction():
                    await session.commit()
            except Exception:
                # 여기까지 온 커밋/플러시 실패는 응답에 반영할 길이 없다(이미 전송됨).
                # 최소한 조용히 사라지지 않게 크게 남긴다.
                await session.rollback()
                logger.error(
                    "db.commit_failed_after_response",
                    path=request.url.path,
                    method=request.method,
                    hint="응답은 이미 전송되어 상태코드를 바꿀 수 없다. 스트리밍 응답 경로.",
                    exc_info=True,
                )
                raise
            # 내부 커밋(핸들러 안 commit)으로 발사된 지연 Redis 쓰기 — 응답이
            # 이미 나간 뒤라 실패는 로그로만 남긴다. drain_pending_redis 보다
            # 먼저 불러야 한다 — 거기서도 지연 태스크를 비워(비-요청 호출자용)
            # surfaced 실패가 warning 으로만 남게 된다(R21 r5 N3).
            from app.core.budget_cache import drain_deferred_redis_writes

            try:
                await drain_deferred_redis_writes(session)
            except Exception:
                logger.error(
                    "deferred_redis_write_failed_after_response",
                    path=request.url.path,
                    exc_info=True,
                )
            await drain_pending_redis(session, request)


class CommittingRoute(APIRoute):
    """응답을 내보내기 **전에** 커밋해서, 커밋 실패가 정상 에러 봉투(500)로 나가게 한다.

    엔드포인트 함수가 반환한 뒤 · 응답이 전송되기 전 지점이 커밋의 유일하게 올바른 위치다.
    의존성의 종료 블록(get_db_session 의 yield 이후)은 이미 응답이 나간 뒤라 늦다.

    직렬화 순서: original(request) 가 이미 response_model 직렬화를 마친 뒤에 커밋한다.
    세션이 expire_on_commit=False 라 커밋 후 객체 접근도 안전하고, 직렬화 중 발생하는
    오류와 커밋 오류가 섞이지 않는다.
    """

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def commit_before_response(request: Request) -> Response:
            response = await original(request)

            session: AsyncSession | None = getattr(request.state, SESSION_STATE_ATTR, None)
            if session is not None:
                # 핸들러가 내부적으로 commit 한 경우라도 그 커밋으로 발사된 지연
                # Redis 쓰기는 응답 전에 완료를 보장한다 — issue_key 의
                # key:vk:{hash} SET 이 응답 뒤에 끝나면 클라이언트의 첫 호출이
                # 401 로 떨어지고, 실패는 24h 짜리 dead credential 이 된다
                # (R21 r3 S1). surface_failure=True 쓰기의 예외는 여기서
                # 올라가 응답을 500 으로 바꾼다 — 클라이언트가 재시도할 수 있다.
                from app.core.budget_cache import drain_deferred_redis_writes

                try:
                    await drain_deferred_redis_writes(session)
                finally:
                    # surfaced 실패가 500 으로 올라가도 이미 커밋된 데이터의
                    # publish 는 발송한다 — 큐에는 after_commit 이벤트로만
                    # enqueue 되므로 미커밋 트랜잭션의 알림은 절대 못 들어온다.
                    # ⚠️ enqueue_redis_publish 를 커밋 전에 직접 부르는 신규
                    #    호출자를 두면 그 불변이 깨진다(R21 r6 N2).
                    await drain_pending_redis(session, request)
                # session.add() 로만 pending 인 객체가 남았다면 flush 로 트랜잭션을
                # 연다(autobegin) — autobegin semantics 에 의존하지 않는다.
                if not session.in_transaction() and (
                    session.new or session.dirty or session.deleted
                ):
                    await session.flush()
            if session is None or not session.in_transaction():
                return response  # DB 를 안 쓰는 엔드포인트이거나 이미 커밋됨

            if isinstance(response, (StreamingResponse, FileResponse)):
                # 본문이 아직 생성되지 않았다(SSE·파일 스트림). 상태줄은 이미 확정이라
                # 여기서 커밋해 실패해도 응답에 반영할 수 없고, 생성기가 세션을 계속
                # 쓸 수도 있다. → get_db_session 의 안전망에 맡긴다.
                return response

            try:
                await session.commit()
            except Exception:
                await session.rollback()
                raise  # 아직 응답 전이므로 예외 핸들러가 정상적으로 500 봉투를 만든다
            # 이번 커밋으로 발사된 지연 Redis 쓰기 — 응답 전 완료 보장(S1).
            # drain_pending_redis 보다 먼저 불러야 한다 — 거기서도 지연 태스크를
            # 비우는데(비-요청 호출자용) 실패를 로그로만 남겨 surfaced 예외가
            # 여기로 올라오지 않는다.
            from app.core.budget_cache import drain_deferred_redis_writes

            try:
                await drain_deferred_redis_writes(session)
            finally:
                await drain_pending_redis(session, request)

            return response

        return commit_before_response


def install_commit_before_response(app: FastAPI) -> int:
    """등록된 모든 APIRoute 를 CommittingRoute 로 승격한다. 승격 개수를 돌려준다.

    왜 라우터마다 `route_class=` 를 주지 않는가: include_router 는 **원본 라우터의**
    route_class 를 보존한다(실측 — app.router.route_class 를 바꿔도 include 된 라우터는
    APIRoute 로 남았다). 라우터가 17개라 개별 지정은 새 라우터가 조용히 빠뜨릴 수 있다.
    앱 조립이 끝난 뒤 한 곳에서 일괄 승격하면 이후 추가되는 라우터도 자동으로 덮인다.

    __class__ 교체 후 route.app 을 다시 만들어야 한다 — APIRoute.__init__ 이
    get_route_handler() 결과를 route.app 에 캐시해 두기 때문이다.
    """
    upgraded = 0
    for route in app.routes:
        if isinstance(route, APIRoute) and not isinstance(route, CommittingRoute):
            route.__class__ = CommittingRoute
            route.app = request_response(route.get_route_handler())
            upgraded += 1
    return upgraded
