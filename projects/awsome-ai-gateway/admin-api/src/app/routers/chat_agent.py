# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""admin-chat-agent 의 admin-api endpoints.

docs/admin-chat-agent-spec.md §4.1 / §4.2 / §4.3 참조.

엔드포인트:
- POST /admin/chat/sessions          — 새 세션 생성
- GET  /admin/chat/sessions          — 사용자의 세션 목록
- GET  /admin/chat/sessions/{id}/history — 메시지 history
- POST /admin/chat/sessions/{id}/messages — 메시지 전송 (SSE stream)
- POST /admin/chat/render-chart      — deterministic chart spec
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from typing import Any, AsyncIterator, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import CurrentUser, require_admin
from app.core.db import AsyncSessionLocal, get_db_session

router = APIRouter(prefix="/admin/chat", tags=["AdminChat"])


logger = logging.getLogger(__name__)

# ─── Config ───
AGENTCORE_RUNTIME_ARN = os.environ.get("AGENTCORE_RUNTIME_ARN", "")
AGENTCORE_REGION = os.environ.get("AGENTCORE_REGION", "ap-northeast-2")
SESSION_TTL_HOURS = 8


# ─── render_chart ───
class ChartRequest(BaseModel):
    """admin-chat-agent (또는 AgentCore Gateway) 에서 호출.

    Code Specialist 가 PNG 를 만들 땐 이 endpoint 가 아니라 S3 presigned URL
    을 반환해서 admin-ui 가 직접 image embed. 이 endpoint 는 SQL 결과의
    recharts spec 변환용.
    """

    kind: Literal["bar", "line", "pie", "area", "table", "kpi"] = Field(
        description="차트 종류"
    )
    data: list[dict[str, Any]] = Field(
        description="renderer 가 그대로 받아 쓸 데이터 (행 array)"
    )
    x: str = Field(description="X 축 컬럼 이름")
    y: str | list[str] = Field(description="Y 축 컬럼 이름 (단일 또는 multi-series)")
    color: str | None = Field(default=None, description="series 분리 컬럼")
    title: str | None = Field(default=None)


class ChartSpec(BaseModel):
    """admin-ui 가 그대로 사용할 chart spec."""

    kind: str
    data: list[dict[str, Any]]
    encoding: dict[str, Any]
    title: str | None = None


@router.post("/render-chart", response_model=ChartSpec)
async def render_chart(
    req: ChartRequest,
    _admin: CurrentUser = Depends(require_admin),
) -> ChartSpec:
    """admin-chat-agent 의 deterministic tool.

    LLM (Viz Specialist) 가 결정한 kind/encoding 과 SQL 결과 (data) 를 받아
    admin-ui 의 ChartRenderer 가 그대로 입력으로 받는 spec 으로 변환.

    spec 자체는 단순한 pass-through 에 가깝지만, 권한 체크 (admin only) 와
    audit log 작성을 보장하기 위해 admin-api 안에서 처리.
    """
    encoding: dict[str, Any] = {"x": req.x, "y": req.y}
    if req.color:
        encoding["color"] = req.color

    return ChartSpec(
        kind=req.kind,
        data=req.data,
        encoding=encoding,
        title=req.title,
    )


# ─── Sessions ───
class SessionCreate(BaseModel):
    pass


class SessionResponse(BaseModel):
    session_id: str
    created_at: str
    expires_at: str


class SessionListItem(BaseModel):
    id: str
    title: str | None
    status: str
    updated_at: str
    message_count: int


@router.post("/sessions", response_model=SessionResponse)
async def create_session(
    _: SessionCreate,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> SessionResponse:
    """새 chat 세션. AgentCore session_id 는 첫 메시지 호출 시 발급 (lazy)."""
    from datetime import datetime, timedelta, timezone

    new_id = str(uuid.uuid4())
    expires = datetime.now(timezone.utc) + timedelta(hours=SESSION_TTL_HOURS)

    await session.execute(
        text(
            """
            INSERT INTO chat_agent.sessions (id, user_id, expires_at)
            VALUES (CAST(:id AS uuid), CAST(:uid AS uuid), :exp)
            """
        ).bindparams(
            id=new_id,
            uid=str(admin.user_id),
            exp=expires,
        )
    )
    await session.commit()

    # 프리워밍(§54): AgentCore microVM 콜드스타트를 사용자의 첫 질문 전에 흡수.
    # __ping__ 은 agent 가 LLM/도구 없이 즉시 done 반환(비용 0). fire-and-forget —
    # 실패해도 세션 생성에 영향 없음(첫 질문이 조금 느릴 뿐).
    if AGENTCORE_RUNTIME_ARN:
        _spawn(_prewarm_agent(new_id))

    return SessionResponse(
        session_id=new_id,
        created_at=datetime.now(timezone.utc).isoformat(),
        expires_at=expires.isoformat(),
    )


async def _prewarm_agent(session_id: str) -> None:
    """세션 생성 직후 AgentCore 를 __ping__ 으로 깨워 microVM 을 선예열."""
    import boto3

    def _ping() -> None:
        client = boto3.client("bedrock-agentcore", region_name=AGENTCORE_REGION)
        resp = client.invoke_agent_runtime(
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=_agentcore_session_id(session_id),
            payload=json.dumps({"content": "__ping__", "session_id": session_id}).encode(),
        )
        body = resp.get("response")
        if hasattr(body, "read"):
            body.read()  # 스트림 소진(커넥션 정리)

    try:
        await asyncio.to_thread(_ping)
        logger.info("agent prewarmed session_id=%s", session_id)
    except Exception as exc:  # noqa: BLE001 — 프리워밍 실패는 무해(로그만)
        logger.warning("agent prewarm failed session_id=%s err=%s", session_id, exc)


@router.get("/sessions")
async def list_sessions(
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> list[SessionListItem]:
    rows = await session.execute(
        text(
            """
            SELECT id, title, status, updated_at, message_count
            FROM chat_agent.sessions
            WHERE user_id = CAST(:uid AS uuid) AND status != 'archived'
            ORDER BY updated_at DESC
            LIMIT 50
            """
        ).bindparams(uid=str(admin.user_id))
    )
    return [
        SessionListItem(
            id=str(r.id),
            title=r.title,
            status=r.status,
            updated_at=r.updated_at.isoformat() if r.updated_at else "",
            message_count=r.message_count,
        )
        for r in rows
    ]


@router.get("/sessions/{session_id}/history")
async def get_history(
    session_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    # 권한 검증 — 자신의 세션만
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id FROM chat_agent.sessions WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=str(session_id))
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    rows = await session.execute(
        text(
            """
            SELECT id, role, content, tool_calls, charts, validator,
                   cost_usd, duration_ms, created_at
            FROM chat_agent.messages
            WHERE session_id = CAST(:sid AS uuid)
            ORDER BY created_at
            """
        ).bindparams(sid=str(session_id))
    )
    return {
        "messages": [
            {
                "id": str(r.id),
                "role": r.role,
                "content": r.content,
                "tool_calls": r.tool_calls,
                "charts": r.charts,
                "validator": r.validator,
                "cost_usd": float(r.cost_usd) if r.cost_usd else None,
                "duration_ms": r.duration_ms,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }


# ─── 핸드오프 릴레이 (§56) ───
# 문제: 기존엔 SSE generator 가 AgentCore 소비+영속화를 겸해, 클라이언트가 페이지를
# 떠나면(스트림 close) 소비도 영속화도 함께 죽었다 — 심층 분석이 "브라우저를 떠나면
# 증발". 해법: AgentCore 소비를 **독립 background task**(producer)로 분리하고, SSE
# 응답은 in-memory 릴레이를 tail 만 한다(consumer). 클라가 끊겨도 producer 는 끝까지
# 돌아 DB 에 영속화 → 사용자가 다른 메뉴에 다녀와도 결과가 history 로 남는다.
# 릴레이는 단일 파드 메모리(현 admin-api replica=1). 멀티 replica 확장 시 Redis
# pub/sub 로 교체 지점.
class _StreamRelay:
    """per-message 이벤트 릴레이. producer 가 append, 소비자는 비동기 tail."""

    def __init__(self) -> None:
        self.frames: list[bytes] = []
        self.done = False
        self._cond = asyncio.Condition()

    async def publish(self, frame: bytes) -> None:
        async with self._cond:
            self.frames.append(frame)
            self._cond.notify_all()

    async def finish(self) -> None:
        async with self._cond:
            self.done = True
            self._cond.notify_all()

    async def tail(self) -> AsyncIterator[bytes]:
        idx = 0
        while True:
            async with self._cond:
                while idx >= len(self.frames) and not self.done:
                    # keepalive 는 호출측(타임아웃)에서 처리
                    try:
                        await asyncio.wait_for(self._cond.wait(), timeout=10.0)
                    except TimeoutError:
                        break
            if idx < len(self.frames):
                while idx < len(self.frames):
                    yield self.frames[idx]
                    idx += 1
            elif self.done:
                return
            else:
                yield b": keepalive\n\n"


# 세션별 활성 릴레이. 새 메시지가 오면 교체(세션당 동시 1개 분석 가정 — UI 가
# isStreaming 중 전송을 막음). 페이지 복귀 시 진행 중 분석을 재구독할 수 있다.
# ⚠️ 프로세스 로컬 — admin-api 가 멀티 replica 면 재구독이 다른 pod 으로 가서
# 404 가 된다. UI 는 그 경우 history 폴링으로 폴백한다(useChatStream, R25-S10).
_active_relays: dict[str, _StreamRelay] = {}

# 세션별 실행 중인 producer task — POST /cancel 이 취소 대상을 찾는 맵.
_active_tasks: dict[str, asyncio.Task] = {}

# 사용자가 명시 취소한 세션 — producer 의 CancelledError 핸들러가 "서버 재시작"
# (lifespan drain)과 "사용자 중단"(cancel endpoint)을 구분하는 플래그.
_cancelled_sessions: set[str] = set()

# fire-and-forget 태스크 강한 참조 — asyncio 는 create_task 결과를 약한 참조로만
# 잡아 GC 되면 분석이 조용히 죽는다(결과 미저장).
_bg_tasks: set[asyncio.Task] = set()

# 영속화(_persist) 태스크 별도 추적 — drain 이 이 태스크들을 cancel 하면 shield
# 보호가 무의미해져 부분/완결 답변이 유실된다(R3-S2: 진입 스냅샷에 이미 있던
# persist 를 다시 cancel 해 실측으로 assistant 행이 사라졌다).
_persist_tasks: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.get_running_loop().create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


def _spawn_persist(coro) -> asyncio.Task:
    """persist 전용 spawn — drain 의 cancel 대상에서 제외되도록 별도 추적."""
    t = _spawn(coro)
    _persist_tasks.add(t)
    t.add_done_callback(_persist_tasks.discard)
    return t


async def drain_chat_bg_tasks(timeout: float = 15.0) -> None:
    """lifespan shutdown — 진행 중 분석 태스크를 취소하고 부분 영속화까지 기다린다.

    pod 종료(롤아웃/스케일인) 시 이 드레인이 없으면 태스크는 루프와 함께 죽어
    부분 답변이 DB 에 남지 않는다. cancel → producer 의 CancelledError 핸들러가
    "서버 재시작" 노트로 영속화 — 그 DB 쓰기가 끝날 때까지 기다린다(배포의
    termination grace 가 이 예산보다 커야 한다).

    취소 대상은 **아직 취소되지 않은 비-persist 태스크뿐** 이다(R3-S2):
    - persist 태스크(`_persist_tasks`)를 cancel 하면 shield 가 의미를 잃고
      진행 중인 DB 쓰기가 롤백돼 완결 답변이 유실된다.
    - 사용자 취소를 이미 처리 중인 producer(`cancelling() > 0`)를 다시
      cancel 하면 핸들러의 `await shield(...)`/`to_thread(...)` 에서 두 번째
      CancelledError 가 올라와(except Exception 에 안 잡힘) persist 없이 빠진다.
    """
    for t in list(_bg_tasks):
        if (
            t.done()
            or t in _persist_tasks
            or t.cancelling() > 0  # 이미 취소 처리 중 — 재취소 금지(py3.11+)
        ):
            continue
        t.cancel()
    deadline = asyncio.get_running_loop().time() + timeout
    while _bg_tasks:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            break
        _done, pending = await asyncio.wait(list(_bg_tasks), timeout=remaining)
        if pending:
            for t in pending:
                # stdlib logger — structlog 식 kwargs(task=)를 넘기면 TypeError(R2-N4).
                logger.warning("chat_bg_task_drain_timeout task=%s", t.get_name())
            break


# ─── 크로스파드 취소/활성 클레임 (R2-S2) ───
# _active_tasks/_cancelled_sessions 는 프로세스 로컬이라, prod(3파드)에서 /cancel 이
# 비소유 파드에 라우팅되면 취소가 무시되고 이중 실행이 열린다. Redis 키로 파드 간
# 공유한다 — redis 가 없으면(dev/테스트) 로컬 맵만으로 동작(degraded).
_ACTIVE_KEY = "chat:active:{}"
_CANCEL_KEY = "chat:cancel:{}"
# 클레임 TTL 은 짧게 + producer 가 주기 갱신(R3-N2). 긴 TTL(960s)이면 소유 파드가
# SIGKILL/OOM 로 죽었을 때 세션이 16분 동안 409 로 잠긴다(취소 플래그를 읽을
# 주체가 없음). producer 가 _CLAIM_REFRESH_SECS 마다 expire 한다 — 죽으면
# TTL 만료로 자연 해제.
_CLAIM_TTL = 120
_CLAIM_REFRESH_SECS = 60
_CANCEL_CHECK_SECS = 1.0


async def _redis_flag(redis, key: str) -> bool:
    """redis EXISTS — 장애 시 False(로컬 가드만으로 동작, 분석을 죽이지 않음)."""
    if redis is None:
        return False
    try:
        return bool(await redis.exists(key))
    except Exception:  # noqa: BLE001
        return False


# ─── Messages (SSE stream proxy) ───
class MessageCreate(BaseModel):
    # kiro R17-7 N-3 — 무제한 user 메시지는 요청마다 이력 SELECT 재조회와 함께
    # 읽기·저장·Bedrock 전송 경로를 증폭한다. 상한을 둔다.
    content: str = Field(max_length=16000)
    # "지금 보는 화면" 컨텍스트(선택). admin-ui 가 페이지 등록 데이터를 동봉 →
    # agent 가 컨텍스트 질의에 활용. {page, period?, data?} 형태. 없으면 None.
    # kiro R17-8 N-h — 직렬화 크기에도 상한을 둔다(user content 와 같은 증폭 계열).
    screen_context: dict | None = None

    @field_validator("screen_context")
    @classmethod
    def _screen_context_size(cls, v: dict | None) -> dict | None:
        if v is not None and len(json.dumps(v, ensure_ascii=False)) > 16000:
            raise ValueError("screen_context too large")
        return v
    # 모드(§55): "quick"(퀵챗 드로어 — 즉답) | "deep"(사이드바 Chat — plan-first
    # 심층분석). agent 가 orchestrator 프로필을 선택한다. 기본 quick(하위호환).
    mode: Literal["quick", "deep"] = "quick"
    # UI 언어 (한/영 토글). agent 가 이 언어로 응답을 생성하도록 payload 에 포함.
    language: Literal["ko", "en"] = "ko"


@router.post("/sessions/{session_id}/messages")
async def post_message(
    request: Request,
    session_id: uuid.UUID,
    req: MessageCreate,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """AgentCore InvokeAgentRuntime 호출 후 SSE 변환.

    - admin 권한은 require_admin 이 검증 → AgentCore 는 admin-api 를 IAM(SigV4)
      으로 신뢰 (런타임이 SigV4 authorizer)
    - 현재 agent 는 단일 JSON 반환 → _agentcore_stream 이 text+done SSE 로 변환
    - admin-api 는 assistant 응답을 chat_agent.messages 테이블에 저장
    """
    if not AGENTCORE_RUNTIME_ARN:
        raise HTTPException(503, "AgentCore runtime not configured")

    sid = str(session_id)
    redis = getattr(request.app.state, "redis", None)
    claim_token = uuid.uuid4().hex
    redis_claimed = False
    # 로컬 클레임 409(남의 진행 중 분석)는 이 relay 를 만든 적이 없으므로 해제하면
    # 안 된다 — 무조건 pop 하면 진행 중인 첫 요청의 릴레이를 뜯어낸다.
    relay: _StreamRelay | None = None
    try:
        # 세션 권한 + 만료 확인 — 클레임(409)보다 먼저 해야 타인 세션의 진행 여부가
        # 409 vs 404/403 으로 새지 않는다(R2-N-k).
        sess_row = (
            await session.execute(
                text(
                    "SELECT user_id, expires_at FROM chat_agent.sessions "
                    "WHERE id = CAST(:sid AS uuid)"
                ).bindparams(sid=sid)
            )
        ).first()
        if not sess_row:
            raise HTTPException(404, "Session not found")
        if str(sess_row.user_id) != str(admin.user_id):
            raise HTTPException(403, "Forbidden")
        # R17 — expires_at 를 조회만 하고 검증하지 않아 만료 세션이 계속 쓰였다.
        # 만료 세션은 410: 프론트가 새 세션을 만들게 유도.
        from datetime import datetime, timedelta, timezone as _tz

        exp = sess_row.expires_at
        if exp is not None:
            if exp.tzinfo is None:
                exp = exp.replace(tzinfo=_tz.utc)
            if exp <= datetime.now(_tz.utc):
                raise HTTPException(410, "Session expired")
            # 슬라이딩 갱신 — 활동 중인 세션이 생성 TTL 에 걸려 대화 도중 죽지 않게
            # 사용할 때마다 만료를 연장한다(kiro R17 #10 — 과거엔 생성 시 한 번만
            # 설정돼 8시간 넘는 분석 세션이 도중에 410 으로 끊겼다).
            await session.execute(
                text(
                    "UPDATE chat_agent.sessions SET expires_at = :exp "
                    "WHERE id = CAST(:sid AS uuid)"
                ).bindparams(
                    exp=datetime.now(_tz.utc) + timedelta(hours=SESSION_TTL_HOURS),
                    sid=sid,
                )
            )

        # 진행 중 분석이 있으면 새 질문 거절 — 그대로 받으면 Q2 가 Q1 뒤에 줄 서서
        # 응답 순서가 뒤섞이고 이중 과금이 된다(R25-S12). Stop(=cancel) 후 재전송.
        #
        # ⚠️ 클레임은 여기서 **동기**로 잡는다 — 예전엔 체크와 등록 사이에 DB
        #    await 가 6번 있어 더블클릭/두 탭 POST 가 둘 다 통과해 고아 producer 가
        #    끝까지 과금됐다(R2-S2b). 이벤트 루프는 단일 스레드라 dict 삽입 자체가
        #    원자다. Redis SET NX 로 파드 간 클레임도 겸한다(비소유 파드로의 409
        #    우회 차단).
        if sid in _active_relays:
            raise HTTPException(
                409, "analysis already running — cancel it first (POST .../cancel)"
            )
        relay = _StreamRelay()
        _active_relays[sid] = relay

        if redis is not None:
            try:
                redis_claimed = bool(
                    await redis.set(
                        _ACTIVE_KEY.format(sid), claim_token, nx=True, ex=_CLAIM_TTL
                    )
                )
            except Exception:  # noqa: BLE001 — redis 장애 시 로컬 가드만(degraded)
                logger.warning("chat active claim failed session_id=%s", sid)
                redis_claimed = True  # 내가 안 잡았으니 finally 에서 지우지 않음
            if not redis_claimed:
                if _active_relays.get(sid) is relay:
                    _active_relays.pop(sid, None)
                raise HTTPException(
                    409, "analysis already running — cancel it first (POST .../cancel)"
                )
            # 이전 런의 취소 플래그가 남아있으면 새 런이 즉시 취소된다 — 제거.
            try:
                await redis.delete(_CANCEL_KEY.format(sid))
            except Exception:  # noqa: BLE001
                pass

        # kiro R17-6 S2 — microVM idle timeout(기본 900s)으로 에이전트 메모리가 죽어도
        # 대화 맥락을 복원할 수 있게 직전 이력을 payload 에 실어 보낸다. 에이전트는
        # messages 가 비어있을 때(새 microVM)만 시드한다. user INSERT **전에** 읽어
        # 방금 보낸 메시지는 제외 — 본문은 invoke 의 content 로 전달되므로 중복 방지.
        # kiro R17-7 N-3 — 원문을 통째로 읽어 Python 에서 자르면 요청마다 최대
        # 12행의 전체 본문이 메모리를 오간다. DB 에서 잘라서 가져온다.
        hist_rows = await session.execute(
            text(
                "SELECT role, left(content, 4000) AS content FROM chat_agent.messages "
                "WHERE session_id = CAST(:sid AS uuid) ORDER BY created_at DESC LIMIT 12"
            ).bindparams(sid=sid)
        )
        history = [
            {"role": r.role, "content": r.content}
            for r in reversed(hist_rows.fetchall())
            if r.role in ("user", "assistant") and r.content
        ]

        # user 메시지 저장
        user_msg_id = str(uuid.uuid4())
        await session.execute(
            text(
                "INSERT INTO chat_agent.messages (id, session_id, role, content) "
                "VALUES (CAST(:id AS uuid), CAST(:sid AS uuid), 'user', :c)"
            ).bindparams(id=user_msg_id, sid=sid, c=req.content)
        )
        # 세션 목록 메타데이터 유지 — 미갱신이면 리스트의 title/카운트/정렬이 전부
        # 생성 시점 상태로 얼어 있었다(R25-N-C8). title 은 첫 질문의 앞부분으로 한 번만.
        await session.execute(
            text(
                "UPDATE chat_agent.sessions SET "
                "updated_at = now(), "
                "message_count = message_count + 1, "
                "title = COALESCE(title, left(:c, 80)) "
                "WHERE id = CAST(:sid AS uuid)"
            ).bindparams(c=req.content, sid=sid)
        )
        await session.commit()
    except BaseException:
        # 권한/만료/기록 단계에서 실패하면 클레임 해제 — 안 풀면 세션이 클레임
        # TTL 동안 409 로 잠긴다. 릴레이·redis 클레임은 **내 것일 때만** 지운다
        # (남의 걸 지우면 그 진행 중 분석이 열린다).
        if relay is not None and _active_relays.get(sid) is relay:
            _active_relays.pop(sid, None)
        if redis is not None and redis_claimed:
            try:
                if await redis.get(_ACTIVE_KEY.format(sid)) == claim_token:
                    await redis.delete(_ACTIVE_KEY.format(sid))
            except Exception:  # noqa: BLE001
                pass
        raise

    # 핸드오프(§56): AgentCore 소비+영속화는 독립 background task 로 — 클라가
    # 페이지를 떠나 SSE 가 끊겨도 분석은 끝까지 돌고 결과는 DB 에 저장된다.
    # SSE 응답은 릴레이 tail(구독)일 뿐. 복귀 시 GET /stream 으로 재구독 가능.
    # relay 는 위에서 이미 클레임됨 — 여기서 다시 만들면 중간 구간의 409 가
    # 다른 relay 를 본다(로컬 클레임 그림자). 여기까지 왔다면 로컬 클레임은 성공.
    assert relay is not None
    _active_tasks[sid] = _spawn(
        _agentcore_producer(
            relay, sid, req.content, req.screen_context, req.mode,
            req.language, history,
            redis=redis, claim_token=claim_token,
        )
    )

    return StreamingResponse(
        relay.tail(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/sessions/{session_id}/stream")
async def reattach_stream(
    session_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> StreamingResponse:
    """진행 중 분석 재구독(§56) — 다른 메뉴에 다녀온 뒤 이어서 스트림.

    릴레이가 살아있으면(분석 진행/방금 완료) 처음부터 tail(이미 발행된 프레임
    재생 + 이후 실시간). 없으면 404 — 클라는 history 로 폴백(완료분은 DB 에 있음).
    """
    sid = str(session_id)
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id FROM chat_agent.sessions WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=sid)
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    relay = _active_relays.get(sid)
    if relay is None:
        raise HTTPException(404, "No active stream")
    # 스트리밍 응답은 요청 스코프 세션의 autobegin 트랜잭션을 스트림 종료까지
    # 붙잡는다(CommittingRoute 는 StreamingResponse 커밋을 생략) — pool 슬롯이
    # idle-in-transaction 으로 고정된다. SELECT 만 했으니 반환 전에 닫는다(R25-N-C4).
    await session.rollback()
    return StreamingResponse(
        relay.tail(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/sessions/{session_id}/cancel")
async def cancel_message(
    request: Request,
    session_id: uuid.UUID,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """진행 중 분석 취소(소유자 전용).

    UI 의 Stop 버튼이 호출한다 — 브라우저 SSE 만 끊으면 서버 측 분석이 끝까지
    토큰을 소비하고 다음 질문이 그 뒤에 줄을 선다. producer task 를 취소하면
    부분 답변은 "[취소됨]" 노트로 영속화된다(R25-S12).
    """
    sid = str(session_id)
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id FROM chat_agent.sessions WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=sid)
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    # 크로스파드(R2-S2a): Service affinity 가 없어 /cancel 이 비소유 파드로 가는
    # 경우가 흔하다 — 로컬 맵만 보면 cancelled:false 가 되어 취소가 유실된다.
    # redis 플래그는 어떤 파드가 받아도 세운다 — 소유 파드의 producer 가 청크
    # 경계(최대 ~1s 지연)에서 읽고 멈춘다. 로컬 task.cancel 은 즉시 경로.
    redis = getattr(request.app.state, "redis", None)
    flag_set = False
    if redis is not None:
        try:
            await redis.set(_CANCEL_KEY.format(sid), "1", ex=_CLAIM_TTL)
            flag_set = True
        except Exception:  # noqa: BLE001
            logger.warning("chat cancel flag write failed session_id=%s", sid)

    task = _active_tasks.get(sid)
    local = task is not None and not task.done()
    if local:
        _cancelled_sessions.add(sid)
        task.cancel()
    # cancelled=true 의 의미는 "취소 신호가 기록됨" 이다 — 유휴 세션에서도
    # redis 플래그만 세워지면 true 가 된다(R3-N4). 실제 취소 완료는 소유 파드의
    # 청크 경계에서 일어나며 UI 는 이 값을 신뢰하지 않는다.
    return {"cancelled": local or flag_set}


# AgentCore runtimeSessionId 는 최소 33자 요구. 우리 세션 UUID(36자)는 충족하지만
# 짧을 가능성에 대비해 패딩.
_AGENTCORE_SESSION_MIN = 33


def _agentcore_session_id(session_id: str) -> str:
    if len(session_id) >= _AGENTCORE_SESSION_MIN:
        return session_id
    return (session_id + "-" + "0" * _AGENTCORE_SESSION_MIN)[:_AGENTCORE_SESSION_MIN]


def _extract_reply(raw: str) -> str:
    """AgentCore 단일 JSON 응답에서 reply 텍스트 추출.

    우리 agent.main.invoke 는 {"reply", "agent", "phase"} dict 반환.
    파싱 실패 시 raw 그대로 반환 (관대).
    """
    try:
        obj = json.loads(raw)
        if isinstance(obj, dict) and "reply" in obj:
            return str(obj["reply"])
    except (ValueError, TypeError):
        pass
    return raw


def _sse(event: str, payload: dict) -> bytes:
    r"""SSE 블록 한 개를 **단일 프레임**으로 직렬화.

    ⚠️ ``event:`` 줄과 ``data:`` 줄을 따로 publish 하면 안 된다. 두 publish 사이에는
    await 경계가 있어 릴레이 소비자(``_StreamRelay.tail``)가 그 틈에 따라잡힐 수 있고,
    그러면 keepalive 코멘트(``: keepalive\n\n``)가 두 줄 **사이에** 끼어 블록이 쪼개진다.
    클라이언트는 그때 ``event: X`` + ``: keepalive``(data 없음 → 폐기)와 ``data: {...}``
    (event 이름 없음 → 'message' 로 오분류) 두 조각을 받아 **이벤트가 통째로 사라진다.**
    한 프레임으로 보내면 구조적으로 쪼개질 수 없다.
    """
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


async def _publish_error(relay: _StreamRelay, message: object, error_type: str) -> None:
    """SSE 에러 프레임 발행.

    ⚠️ 본문 키는 ``error_type`` 이다 — **``type`` 이면 안 된다.** admin-ui 의
    ``parseSseBlock`` 은 프레임을 ``{type: <event 이름>, ...payload}`` 로 합치므로,
    본문에 ``type`` 이 들어있으면 그게 이벤트 이름 ``'error'`` 를 덮어쓴다
    (예: ``type: 'ClientError'``). 그러면 ChatLayout 의 ``case 'error'`` 가 잡지
    못하고 ``default: return msg`` 로 **조용히 버려진다** — 사용자에게는 실패가
    전혀 보이지 않고 pending 스피너만 영구히 돌았다
    (admin-ui/src/components/chat/ChatLayout.tsx:415, types.ts StreamEvent).
    """
    await relay.publish(_sse("error", {"error": message, "error_type": error_type}))


async def _agentcore_producer(
    relay: _StreamRelay,
    session_id: str,
    content: str,
    screen_context: dict | None = None,
    mode: str = "quick",
    language: str = "ko",
    history: list[dict] | None = None,
    redis=None,
    claim_token: str | None = None,
) -> None:
    """AgentCore InvokeAgentRuntime → 릴레이 발행 + DB 영속화 (background, §56).

    핸드오프 핵심: 이 task 는 **클라이언트 SSE 연결과 독립**이다. 사용자가 다른
    메뉴로 이동해 스트림이 끊겨도 AgentCore 소비·영속화는 끝까지 진행되고,
    완료분은 chat_agent.messages 에 저장된다(복귀 시 history/재구독으로 복원).
    DB 는 요청 스코프 세션 대신 **자체 AsyncSessionLocal** 사용(요청 세션은
    응답 반환 후 닫히므로 background 에서 쓰면 안 됨).

    인증: 런타임이 SigV4(IAM) authorizer 이므로 admin-api 의 IAM 자격증명
    (EKS IRSA role) 으로 호출. admin 권한은 post_message 진입 시 검증됨.
    """
    import boto3
    from botocore.config import Config as BotoConfig

    payload = {"content": content, "session_id": session_id, "mode": mode, "language": language}
    if screen_context:
        payload["screen_context"] = screen_context
    if history:
        payload["history"] = history

    accumulated: list[str] = []
    # 영속화 누적: 스트림 종료 후 chat_agent.messages 에 저장해 새로고침/재조회
    # 시에도 SQL/차트/검증이 보이도록. UI applyEvent 와 **같은 형태**로 재구성:
    #   tool_calls = [{tool, args, result, status:'done'}] (tool_call+result 병합)
    #   charts = [spec, ...], validator = 마지막 단일 객체.
    # (이전엔 content 만 저장 → tool_calls/charts/validator 컬럼이 NULL 로 남아
    #  read 경로는 조회하는데 표시할 게 없었음.)
    # ⚠️ 이 선언들은 try **밖**이어야 한다 — 아래 except 핸들러가 부분 결과를
    #    영속화하므로, try 안에서 선언하면 invoke 전에 터진 예외(자격증명/네트워크)
    #    에서 핸들러가 NameError 로 다시 죽어 아무것도 남지 않는다.
    acc_tool_calls: list[dict] = []
    acc_charts: list[dict] = []
    acc_validator: dict | None = None
    # report 이벤트 카드. chat_agent.messages 에는 reports 컬럼이 없어 **영속화되지
    # 않는다** — 세션 중에만 표시된다(복원까지 원하면 컬럼 추가가 필요).
    acc_reports: list[dict] = []
    # 종료 판정 플래그.
    #   saw_done      — agent 가 마지막 {"type":"done"} 프레임을 보냈는가
    #   stream_error  — in-band 에러 프레임을 이미 발행했는가(중복 보고 방지)
    #   streamed      — 스트리밍 분기를 탔는가(비스트리밍 fallback 엔 done 프레임 없음)
    saw_done = False
    stream_error = False
    streamed = False
    persisted = False
    # 사용자 취소 여부 — 로컬 task.cancel(_cancelled_sessions) 또는 크로스파드
    # redis 플래그(_CANCEL_KEY) 경로 모두 여기로 모은다.
    cancelled_by_user = False
    # 상류 정리용 참조 — CancelledError/finally 에서 body.close() 와 prefetch
    # future 정리에 쓴다(R2-S1: 안 닫으면 취소 후에도 AgentCore 가 과금 계속).
    client = None
    body = None
    pending: asyncio.Task | None = None
    # 정상 경로 persist 를 추적 — drain/취소가 이 write 도중에 오면 바깥 태스크만
    # 끊기고 inner persist 가 고아가 되거나 핸들러가 중복 persist 를 시작한다
    # (R2-N-a/b). 태스크를 잡아두고 핸들러가 같은 태스크를 await 한다.
    persist_task: asyncio.Task | None = None
    # stop_runtime_session 중복 호출 가드(R4-N4) — 크로스파드 취소 경로가 이미
    # 호출한 뒤 drain cancel 이 오면 핸들러가 다시 호출했다(첫 호출은 to_thread
    # 워커에 버려지고 두 번째가 또 RTT 를 탄다).
    runtime_stopped = False

    # 클레임 heartbeat(R4-N3) — producer 수명 전체를 커버. 스트림 루프 안에서만
    # 갱신하면 invoke(blocking 최대 900s)나 비스트리밍 fallback 구간에 TTL 120s
    # 가 만료돼 같은 세션이 다른 파드에서 이중 실행된다. 내 토큰일 때만 연장.
    claim_refresher: asyncio.Task | None = None
    if redis is not None and claim_token:
        async def _claim_heartbeat() -> None:
            akey = _ACTIVE_KEY.format(session_id)
            while True:
                await asyncio.sleep(_CLAIM_REFRESH_SECS)
                try:
                    await redis.eval(
                        "if redis.call('get', KEYS[1]) == ARGV[1] "
                        "then return redis.call('expire', KEYS[1], ARGV[2]) "
                        "else return 0 end",
                        1,
                        akey,
                        claim_token,
                        _CLAIM_TTL,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001 — 최선노력 갱신
                    pass

        claim_refresher = _spawn(_claim_heartbeat())

    async def _persist(error_note: str | None = None, *, label: str = "오류") -> None:
        """스트림 누적분을 chat_agent.messages 에 저장. 성공·실패 **양쪽**에서 호출.

        ⚠️ 예전엔 이 저장이 try 안, 스트림 루프 **뒤**에만 있었다 — AgentCore 예외가
        나면 이미 흘려보낸 텍스트·도구호출·차트까지 전부 버려져 assistant 턴이 통째로
        사라졌다(새로고침하면 질문만 남고 답이 없다). 부분 답변이라도 남긴다.

        ``error_note`` 가 주어지면 실패 종료로 취급 — 본문에 라이브 화면과 **같은**
        ``[{label}] …`` 문구를 덧붙이고(복원 화면이 라이브와 일치하도록) 미완 도구를
        failed 로 정리한다. ``label``: 사용자 취소는 '취소됨'(오류처럼 보이지 않게),
        서버 중단은 '중단', 그 외 실패는 '오류'(R2-N5).

        ⚠️ ``persisted`` 는 commit **뒤**에 세운다(R2-N3). 쓰기 전에 세우면 정상
        경로의 persist 도중 cancel(drain/Stop)이 와도 CancelledError 핸들러의
        재시도가 `persisted=True` 를 보고 조기 리턴 — 완결된 답변 행이 유실된다.
        커밋과 플래그 사이엔 await 가 없어 이중 쓰기 창도 없다(취소가 쓰기
        중간에 오면 트랜잭션은 롤백되고, 핸들러가 flag=False 인 채로 다시 쓴다).
        """
        nonlocal persisted
        if persisted:
            return
        # tool_result 없는 도구(render_chart 등)가 running 으로 영속화되면 복원
        # 시 "실행 중..." 고착(§57) — 정상 종료면 done, 실패 종료면 failed.
        for tc in acc_tool_calls:
            if tc.get("status") == "running":
                tc["status"] = "failed" if error_note else "done"
        full_reply = "".join(accumulated)
        # [SUGGESTIONS]...[/SUGGESTIONS] 마커는 UI 전용(후속질문 칩) — DB 본문에서
        # 제거(§55). UI 도 동일 정규식으로 추출·제거하므로 양쪽 일관.
        full_reply = re.sub(
            r"\s*\[SUGGESTIONS\][\s\S]*?\[/SUGGESTIONS\]\s*", "", full_reply
        ).rstrip()
        if error_note:
            note = f"\n\n[{label}] {error_note}"
            # content 저장 상한이 8000 자다. 그냥 이어붙이면 긴 답변에서 **오류 문구가
            # 먼저 잘려** 실패 사실이 사라지므로, 본문 쪽에서 자리를 비운다.
            full_reply = (full_reply[: 8000 - len(note)].rstrip() + note).lstrip()
        # jsonb 컬럼은 None 이면 SQL NULL, 있으면 JSON 문자열을 ::jsonb 캐스팅.
        # 빈 리스트는 저장 안 함(NULL) → read 경로가 undefined 로 매핑(UI 와 일관).
        async with AsyncSessionLocal() as own_db:
            await own_db.execute(
                text(
                    "INSERT INTO chat_agent.messages "
                    "(session_id, role, content, tool_calls, charts, validator) "
                    "VALUES (CAST(:sid AS uuid), 'assistant', :c, "
                    "CAST(:tc AS jsonb), CAST(:ch AS jsonb), CAST(:vd AS jsonb))"
                ).bindparams(
                    sid=session_id,
                    c=full_reply[:8000],
                    tc=json.dumps(acc_tool_calls) if acc_tool_calls else None,
                    ch=json.dumps(acc_charts) if acc_charts else None,
                    vd=json.dumps(acc_validator) if acc_validator else None,
                )
            )
            # 세션 목록 메타데이터 — assistant 턴도 카운트/정렬에 반영(R25-N-C8).
            await own_db.execute(
                text(
                    "UPDATE chat_agent.sessions SET "
                    "updated_at = now(), message_count = message_count + 1 "
                    "WHERE id = CAST(:sid AS uuid)"
                ).bindparams(sid=session_id)
            )
            await own_db.commit()
            persisted = True

    try:
        # read_timeout 900s — boto3 기본(60s)이면 deep 분석의 AgentCore flush 간격
        # (§51: sub-agent blocking 중 최대 ~60s+)에 ReadTimeout 으로 producer 가
        # 중간에 죽어 결과가 증발한다(§56 실측). retries=0: 재시도하면 같은 질문이
        # agent 에 중복 invoke 됨.
        client = boto3.client(
            "bedrock-agentcore",
            region_name=AGENTCORE_REGION,
            config=BotoConfig(
                read_timeout=900, connect_timeout=15, retries={"max_attempts": 0}
            ),
        )
        # boto3 호출은 blocking → 이벤트 루프 양보 위해 thread 로
        response = await asyncio.to_thread(
            client.invoke_agent_runtime,
            agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
            runtimeSessionId=_agentcore_session_id(session_id),
            payload=json.dumps(payload).encode(),
        )

        # SigV4 응답 본문은 'response' (StreamingBody). 구버전 'payload' fallback.
        body = response.get("response") or response.get("payload")

        # Phase 4: agent 가 async-generator 면 AgentCore 가 `data: <json>\n\n` 프레임을
        # 점진적으로 흘린다. StreamingBody.iter_lines() 로 한 줄씩 읽어 admin-ui SSE 로
        # 재발행. 각 next() 는 blocking 이라 to_thread 로 감싸 이벤트 루프 양보(전체
        # 루프를 감싸면 재버퍼링되므로 next() 단위로만).
        if hasattr(body, "iter_chunks") or hasattr(body, "iter_lines"):
            streamed = True
            # iter_lines(chunk_size=1) 은 라인 한 줄마다 bytes 재할당이 누적되어
            # O(L²) 다 — 300KB 라인(대형 tool_result)에 ~10s, 그 사이 이 pod 의
            # 다른 요청이 굶는다(R25-N-C5). iter_chunks 는 도착한 만큼 돌려주므로
            # 첫 프레임 지연 없이(1바이트 시대의 목적 유지) 라인 분할은 우리가 한다.
            # iter_lines 폴백은 개행을 **떼고** 라인을 돌려준다 — 아래 라인 분할은
            # '\n' 기준이므로 폴백 경로에선 개행을 복원해 줘야 프레임이 뭉개지지
            # 않는다(뭉개지면 마지막 꼬리에서 통째로 json.loads 실패 → 모두 text).
            use_iter_lines = not hasattr(body, "iter_chunks")
            chunk_iter = (
                body.iter_lines(chunk_size=4096)
                if use_iter_lines
                else body.iter_chunks(4096)
            )

            def _next_chunk():
                return next(chunk_iter, None)

            # heartbeat: 다음 청크를 기다리는 동안 HEARTBEAT_SECS 마다 SSE 코멘트
            # (`: keepalive`)를 흘려 (1) 연결 idle drop 방지 (2) 클라가 살아있음 인지.
            # 5-agent 파이프라인의 긴 침묵(SQL/Validator 실행) 구간 대응.
            # next() 를 한 번만 to_thread 로 띄워두고, 완료될 때까지 timeout 으로 폴링.
            HEARTBEAT_SECS = 10.0
            pending = asyncio.ensure_future(asyncio.to_thread(_next_chunk))
            line_buf = b""
            eof = False
            stop_stream = False
            last_cancel_check = 0.0
            while not stop_stream:
                # 크로스파드 취소(R2-S2a): /cancel 이 다른 파드로 라우팅돼도
                # redis 플래그를 세우므로 여기서 읽는다. 청크마다 묻지 않고
                # 1초에 한 번(heartbeat 경계 포함) — 라운드트립 비용 절감.
                # R3-N3 — 이 체크가 청크 대기 상한(HEARTBEAT_SECS=10)과 묶여 있으면
                # 취소 지연이 최대 ~10s 가 됐다. wait_for 상한을 취소 주기로 내린다.
                if redis is not None and time.monotonic() - last_cancel_check >= _CANCEL_CHECK_SECS:
                    last_cancel_check = time.monotonic()
                    if await _redis_flag(redis, _CANCEL_KEY.format(session_id)):
                        cancelled_by_user = True
                        stop_stream = True
                        break
                try:
                    chunk = await asyncio.wait_for(
                        asyncio.shield(pending), _CANCEL_CHECK_SECS
                    )
                except asyncio.TimeoutError:
                    # 다음 청크 대기 — keepalive 는 relay.tail() 이 자체 발행
                    continue
                if chunk is None:
                    # 개행 없이 끝난 꼬리 조각도 한 번 처리하고 종료한다 —
                    # 끊긴 스트림의 마지막 프레임이 여기 있을 수 있다.
                    if not line_buf.strip():
                        break
                    lines = [line_buf]
                    line_buf = b""
                    eof = True
                else:
                    # 다음 청크 prefetch 시작 (처리와 병행)
                    pending = asyncio.ensure_future(asyncio.to_thread(_next_chunk))
                    piece = (
                        chunk
                        if isinstance(chunk, (bytes, bytearray))
                        else str(chunk).encode()
                    )
                    if use_iter_lines:
                        piece += b"\n"  # iter_lines 가 떼어낸 개행 복원
                    line_buf += piece
                    # 완성된 라인만 떼어 처리 — 마지막 조각은 다음 청크와 합쳐진다.
                    lines = line_buf.split(b"\n")
                    line_buf = lines.pop()
                for line in lines:
                    text_line = line.decode("utf-8", errors="replace")
                    if not text_line.startswith("data:"):
                        continue
                    data_str = text_line[len("data:"):].strip()
                    if data_str in ("", "[DONE]"):
                        continue
                    try:
                        evt = json.loads(data_str)
                    except (ValueError, TypeError):
                        evt = {"type": "text", "chunk": data_str}

                    # AgentCore in-band 에러 프레임 (HTTP 는 200 유지)
                    if isinstance(evt, dict) and "error_type" in evt:
                        await _publish_error(
                            relay,
                            evt.get("error", "stream error"),
                            str(evt.get("error_type") or "StreamError"),
                        )
                        stream_error = True
                        stop_stream = True
                        break

                    etype = evt.get("type") if isinstance(evt, dict) else None
                    if etype == "chart" and isinstance(evt.get("spec"), dict):
                        # strip: 표시 텍스트에서 제거할 원문 JSON 블록(admin-ui 가 사용)
                        payload_out = {"spec": evt["spec"]}
                        if evt.get("strip"):
                            payload_out["strip"] = evt["strip"]
                        acc_charts.append(evt["spec"])  # 영속화: charts[]
                        await relay.publish(_sse("chart", payload_out))
                    elif etype == "thinking":
                        # "작업 중" 신호 — 본문에 누적하지 않고 그대로 전달
                        await relay.publish(_sse("thinking", {"text": evt.get("text", "")}))
                    elif etype == "reasoning":
                        # 추론 요약 델타(orchestrator display:summarized) — 침묵 구간을
                        # 메우는 "사고 과정" 스트림. 본문(accumulated)에 누적하지 않고
                        # 그대로 전달(답변과 분리, DB 영속화 대상 아님).
                        await relay.publish(_sse("reasoning", {"chunk": evt.get("chunk", "")}))
                    elif etype == "tool_call":
                        # 도구 호출 투명성 (어떤 specialist 를 부르는지)
                        tool_name = evt.get("tool", "")
                        # 영속화: UI applyEvent 와 동일하게 running 상태로 추가
                        acc_tool_calls.append(
                            {"tool": tool_name, "args": evt.get("args", {}), "status": "running"}
                        )
                        await relay.publish(
                            _sse("tool_call", {"tool": tool_name, "args": evt.get("args", {})})
                        )
                    elif etype == "tool_result":
                        # 실행된 코드/구조화 결과 (Code Specialist 의 code 포함)
                        tool_name = evt.get("tool", "")
                        tool_result = evt.get("result", {})
                        # 영속화: 같은 이름 중 **result 가 아직 없는 첫 항목**에 채움(§57 fix).
                        # 끝에서 스캔하면 같은 이름 호출 N개가 모두 마지막 항목에 덮어써져
                        # 앞 항목들이 영원히 running 으로 남는다(복원 시 "실행 중..." 고착).
                        for i in range(len(acc_tool_calls)):
                            if (
                                acc_tool_calls[i]["tool"] == tool_name
                                and "result" not in acc_tool_calls[i]
                            ):
                                acc_tool_calls[i]["result"] = tool_result
                                acc_tool_calls[i]["status"] = "done"
                                break
                        else:
                            acc_tool_calls.append(
                                {"tool": tool_name, "result": tool_result, "status": "done"}
                            )
                        await relay.publish(
                            _sse("tool_result", {"tool": tool_name, "result": tool_result})
                        )
                    elif etype == "validator":
                        # reconciliation gate WARN 등. 영속화: 마지막 verdict 단일 객체.
                        acc_validator = evt.get("result", {})
                        await relay.publish(_sse("validator", {"result": evt.get("result", {})}))
                    elif etype == "verification":
                        # L3 실행기반 후보선택 검증 메타(§58, deep 모드만). agreement/k/
                        # verdict 를 그대로 전달 — UI 가 "검증됨" 카드로 렌더(설명가능성).
                        # 휘발성 진행표시라 DB 영속화 안 함(validator 와 동일 취급).
                        await relay.publish(
                            _sse("verification", {"result": evt.get("result", {})})
                        )
                    elif etype == "audit":
                        # L5 독립 답변 감사(§60, deep+고위험만). 최종 산문 수치 cite 무결성
                        # verdict/defects 를 그대로 전달 — UI advisory 카드(비파괴). validator
                        # 와 동일하게 휘발성 진행표시로 취급(DB 영속화 안 함).
                        await relay.publish(_sse("audit", {"result": evt.get("result", {})}))
                    elif etype == "heartbeat":
                        # 공백 없는 스트리밍 생존신호(진행 단계/경과시간). 본문(accumulated)에
                        # 절대 누적하지 않고 그대로 전달 — DB 영속화 대상 아님(휘발성 진행표시).
                        # ⚠️ 명시 elif 필수: 없으면 아래 else 가 'phase'/'label' 없는 dict 라
                        # chunk 추출 실패로 무시되거나, 키가 겹치면 본문 오염. (transport-level
                        # `: keepalive` SSE 코멘트와는 다른, 데이터 이벤트.)
                        await relay.publish(
                            _sse(
                                "heartbeat",
                                {
                                    "phase": evt.get("phase", ""),
                                    "label": evt.get("label", ""),
                                    "elapsed_ms": evt.get("elapsed_ms", 0),
                                },
                            )
                        )
                    elif etype == "plan":
                        # deep 모드 분석 계획(§57 PlanCard). strip 으로 본문에서 raw
                        # JSON 펜스 제거(차트와 동일 패턴). 영속화는 본문 텍스트에
                        # 펜스가 남으므로 별도 컬럼 불필요(복원 시 UI 가 재추출).
                        payload_out = {"plan": evt.get("plan", {})}
                        if evt.get("strip"):
                            payload_out["strip"] = evt["strip"]
                        await relay.publish(_sse("plan", payload_out))
                    elif etype == "report":
                        # 다운로드 리포트 카드. s3_uri 는 그대로 전달(UI 가 다운로드 클릭 시
                        # /reports/download 로 presign 요청 — URL 을 미리 굽지 않음, 만료·검증
                        # 우회 방지). ⚠️ chat_agent.messages 에 reports 컬럼이 없어
                        # acc_reports 는 **DB 에 저장되지 않는다** — 새로고침하면 카드가
                        # 사라진다(세션 중에만 유효). 복원까지 원하면 컬럼 추가가 필요.
                        rep = {
                            "s3_uri": evt.get("s3_uri", ""),
                            "file_name": evt.get("file_name", "report"),
                            "format": evt.get("format", "pdf"),
                            "summary": evt.get("summary", ""),
                            "page_count": evt.get("page_count"),
                        }
                        acc_reports.append(rep)
                        await relay.publish(_sse("report", rep))
                    elif etype == "done":
                        # agent 가 보내는 **정상 완료 표식**. 이 프레임을 못 보고 스트림이
                        # 끝나면 절단이다(아래 truncated 판정) — 여기서만 기록하고 발행은
                        # 루프 종료 후 한 번에 한다.
                        saw_done = True
                        continue
                    else:
                        chunk = (
                            (evt.get("chunk") or evt.get("delta") or evt.get("reply") or "")
                            if isinstance(evt, dict)
                            else str(evt)
                        )
                        if chunk:
                            accumulated.append(chunk)
                            await relay.publish(_sse("text", {"chunk": chunk}))
                if eof or stop_stream:
                    break
            # 루프 탈출(EOF/in-band 에러/cancel) 시 prefetch 해 둔 _next_chunk future 가
            # 남을 수 있다 — to_thread 의 워커 스레드는 취소로 멈추지 않지만 future 를
            # cancel 해 두면 "Future exception was never retrieved" 경고와 참조 누수를
            # 막는다(이미 끝난 future 에 대한 cancel 은 no-op).
            if not pending.done():
                pending.cancel()
        else:
            # Fallback: 비스트리밍 단일 JSON 응답
            raw = body.read().decode() if hasattr(body, "read") else json.dumps(response, default=str)
            reply = _extract_reply(raw)
            accumulated.append(reply)
            await relay.publish(_sse("text", {"chunk": reply}))

        # 스트림이 agent 의 done 프레임 없이 끝났으면 **성공이 아니다.** iter_lines 가
        # None 을 돌려주는 경로(=루프의 `if line is None: break`)는 런타임 종료·네트워크
        # 절단·read timeout 으로 프레임이 중간에 끊긴 경우도 포함한다 — agent 는 정상
        # 완료 시 반드시 마지막에 {"type":"done"} 을 yield 하므로
        # (admin-chat-agent/src/agent/main.py:1601) 그 부재가 절단의 신호다.
        # 예전엔 이 경로에서도 `event: done` 만 발행해 **잘린 답변이 완결된 답변으로**
        # 표시됐다(사용자는 조용히 truncate 된 분석을 신뢰하게 된다).
        truncation_note: str | None = None
        if cancelled_by_user:
            # 크로스파드 취소(R2-S2a) — truncated 가 아니라 사용자 취소다.
            # 상류도 끊는다: body.close(finally)+stop_runtime_session(R2-S1).
            cancel_note = "사용자가 분석을 중단했습니다"
            await _publish_error(relay, cancel_note, "Cancelled")
            truncation_note = cancel_note
            persist_label = "취소됨"
            if client is not None:
                # 호출 전에 세운다 — 이 await 도중 drain cancel 이 오면 핸들러가
                # 다시 호출하는데, to_thread 요청은 워커에서 어차피 계속 돈다.
                runtime_stopped = True
                try:
                    await asyncio.to_thread(
                        client.stop_runtime_session,
                        agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
                        runtimeSessionId=_agentcore_session_id(session_id),
                    )
                except Exception:  # noqa: BLE001 — 최선노력
                    logger.warning(
                        "chat stop_runtime_session failed session_id=%s", session_id
                    )
        elif streamed and not saw_done and not stream_error:
            truncation_note = (
                "스트림이 완료 신호 없이 끊겼습니다 — 답변이 중간에 잘렸을 수 있습니다."
            )
            await _publish_error(relay, truncation_note, "StreamTruncated")
            persist_label = "오류"
        else:
            persist_label = "오류"

        # done 은 "성공"이 아니라 "스트림 종료" 신호다(실패 사실은 위 error 프레임이
        # 전달). 클라이언트가 pending 스피너를 정리하고 [SUGGESTIONS] 를 추출하려면
        # 절단·에러 종료에서도 필요하다.
        await relay.publish(_sse("done", {}))

        # 정상 경로도 shield(R2-N-a): DB 쓰기 도중 drain/Stop cancel 이 오면
        # 완결된 답변이 '중단' 노트로 재기록되거나 통째로 유실된다. _spawn 으로
        # _bg_tasks 에 등록해야 drain 이 이 쓰기까지 기다린다(R2-N-b).
        persist_task = _spawn_persist(
            _persist(error_note=truncation_note, label=persist_label)
        )
        await asyncio.shield(persist_task)

    except asyncio.CancelledError:
        # ⚠️ CancelledError 는 BaseException 이라 위 except Exception 에 잡히지
        # 않는다 — pod 종료(lifespan drain)·사용자 취소 모두 이 경로. 부분 답변을
        # 영속화하지 않으면 화면에 흘러간 텍스트가 DB 에 하나도 남지 않는다.
        # shield 로 감싼다: drain 이 태스크를 다시 cancel 해도 persist 는 완주.
        # set.discard 는 항상 None 을 반환 — 멤버십을 먼저 본다.
        user_cancelled = (
            session_id in _cancelled_sessions
            or await _redis_flag(redis, _CANCEL_KEY.format(session_id))
        )
        _cancelled_sessions.discard(session_id)
        cancelled_by_user = cancelled_by_user or user_cancelled
        if persist_task is not None:
            # R3-N1 — 정상 경로 persist 가 이미 돌고 있다 = 답변은 완결.
            # 취소 프레임을 보내거나 이미 끝난 런에 stop_runtime_session 을
            # 호출하면 라이브 화면(취소됨)과 DB(정상 답변)가 어긋난다.
            # 여기선 그 태스크의 완주만 기다리고 조용히 종료한다.
            try:
                await asyncio.shield(persist_task)
            except Exception:  # noqa: BLE001 — 실패면 아래에서 재시도
                pass
            if persisted:
                raise
        cancel_note = (
            "사용자가 분석을 중단했습니다"
            if user_cancelled
            else "서버 재시작으로 분석이 중단되었습니다"
        )
        # 상류 끊기(R2-S1): task.cancel 만으로는 to_thread 워커의 HTTP 연결이
        # 살아있어 AgentCore 가 끊긴 줄 모르고 토큰을 계속 소비한다(_ORCH_LOCK
        # 도 점유). body.close() 로 소켓을 끊고, 사용자 취소면 런타임 세션 자체를
        # 멈춘다 — 없으면 취소한 질문이 끝까지 과금된다.
        if pending is not None and not pending.done():
            pending.cancel()
        if body is not None:
            try:
                body.close()
            except Exception:  # noqa: BLE001
                pass
        # 크로스파드 취소 경로에서 이미 stop 을 호출했다면 건너뛴다(R4-N4 —
        # 그 호출 도중 drain cancel 이 와도 두 번째 RTT 를 타지 않게).
        if user_cancelled and client is not None and not runtime_stopped:
            try:
                await asyncio.to_thread(
                    client.stop_runtime_session,
                    agentRuntimeArn=AGENTCORE_RUNTIME_ARN,
                    runtimeSessionId=_agentcore_session_id(session_id),
                )
            except Exception:  # noqa: BLE001 — 최선노력, 취소 전파를 막지 않는다
                logger.warning(
                    "chat stop_runtime_session failed session_id=%s", session_id
                )
        try:
            # 구독자에게도 종료 사유를 알린다 — 그냥 EOF 면 UI 는 done 없는
            # 스트림 종료를 "미완성" 으로 처리한다(R25-N-C7 과 정합).
            await _publish_error(relay, cancel_note, "Cancelled")
        except Exception:  # noqa: BLE001
            pass
        try:
            # 정상 경로 persist 가 이미 돌고 있으면 새 persist 를 시작하지 않고
            # 그 태스크 완주를 기다린다 — 새로 시작하면 flag=False 창에 이중 INSERT
            # (R2-N-a/b). 진행 중인 게 없으면 취소 노트로 새로 쓴다.
            if persist_task is not None and not persist_task.done():
                await asyncio.shield(persist_task)
            if not persisted:
                await asyncio.shield(
                    _spawn_persist(
                        _persist(
                            error_note=cancel_note,
                            label="취소됨" if user_cancelled else "중단",
                        )
                    )
                )
        except Exception:  # noqa: BLE001 — 취소 전파를 막지 않는다
            logger.exception("chat cancelled persist failed session_id=%s", session_id)
        raise

    except Exception as exc:
        await _publish_error(relay, str(exc), type(exc).__name__)
        # ⚠️ 예외 경로에서도 영속화한다 — 안 하면 이미 사용자 화면에 흘러간 부분
        # 답변·도구호출이 DB 에 하나도 남지 않아, 새로고침하면 assistant 턴이 통째로
        # 사라진다(질문만 남는다). 저장 자체가 또 실패해도 원래 예외 보고를 덮지
        # 않도록 따로 감싼다.
        try:
            # R4-N2 — 예외 경로 persist 도 shield+등록: 이 commit 도중
            # SIGTERM(drain)/Stop 이 오면 CancelledError 가 rollback 을 일으켜
            # 부분 답변이 유실된다(나머지 두 경로와 같은 패턴).
            await asyncio.shield(
                _spawn_persist(_persist(error_note=f"{type(exc).__name__}: {exc}"))
            )
        except asyncio.CancelledError:
            # outer 가 취소된 것 — shield 된 persist 는 계속 돈다.
            raise
        except Exception:  # noqa: BLE001 — 영속화 실패가 에러 보고를 가리지 않게
            # stdlib logging — structlog 식 kwargs 를 넘기면 TypeError 다(%s 포맷).
            logger.exception("chat partial persist failed session_id=%s", session_id)
    finally:
        # 상류·prefetch 정리(R2-S1): 정상/에러/취소 전 경로에서 소켓을 닫는다 —
        # 안 닫으면 done 후에도 FIN_WAIT 로 소켓이 남고, 취소 경로에선 AgentCore 가
        # 끝까지 과금된다.
        if claim_refresher is not None:
            claim_refresher.cancel()
        if pending is not None and not pending.done():
            pending.cancel()
        if body is not None:
            try:
                body.close()
            except Exception:  # noqa: BLE001
                pass
        # tail 구독자 종료 신호 + 릴레이·태스크 정리(완료 후 재구독은 history 폴백).
        await relay.finish()
        if _active_relays.get(session_id) is relay:
            _active_relays.pop(session_id, None)
        # 자기 자신이 등록된 태스크면 해제 — finally 시점엔 아직 done()=False 다.
        if _active_tasks.get(session_id) is asyncio.current_task():
            _active_tasks.pop(session_id, None)
        _cancelled_sessions.discard(session_id)
        # 크로스파드 클레임 해제 — 내 토큰일 때만. 취소 플래그도 함께 지운다
        # (다음 질문이 과거 취소로 즉시 멈추지 않게 — post_message 도 클레임
        # 시 지우지만 레이스 여지가 남으므로 양쪽에서 정리).
        if redis is not None:
            try:
                akey = _ACTIVE_KEY.format(session_id)
                if claim_token is not None and await redis.get(akey) == claim_token:
                    await redis.delete(akey)
                await redis.delete(_CANCEL_KEY.format(session_id))
            except Exception:  # noqa: BLE001
                pass


# ─── 인라인 SQL 재실행 (§57 Phase2-B — deep-insight 차용) ───
# UI 의 SQL 블록에서 분석가가 SQL 을 보고/수정해 **LLM 미경유**로 재실행(ms~s 단위,
# 토큰 비용 0). 보안: query_db Lambda 의 Layer A 검증 스택(sqlglot AST + 테이블
# 화이트리스트 + SELECT-only + EXPLAIN cost + LIMIT 강제 + read-only role +
# statement_timeout)을 **그대로 재사용** — admin-api 는 검증 로직을 중복 구현하지
# 않고 Lambda 에 위임한다. require_admin + 세션 소유 검증.
# R16: dev Lambda 이름을 전 환경 기본값으로 갖고 있었다 — prod 에서 env 누락 시
# dev 리소스를 조용히 호출한다. 빈 값이면 엔드포인트가 503 으로 닫힌다(fail-safe).
LAMBDA_QUERY_DB = os.environ.get("LAMBDA_QUERY_DB", "")


class _LambdaFunctionError(RuntimeError):
    """Lambda invoke 의 FunctionError(핸들러 예외/타임아웃) — HTTP 200 위장 방지."""


class SqlExecuteRequest(BaseModel):
    sql: str
    # uuid.UUID 타입으로 두면 비-UUID 입력은 FastAPI 가 422 로 거절한다 —
    # str + CAST 는 SQL 에서 DataError → 500 이 됐다(R25-N-C9).
    session_id: uuid.UUID


@router.post("/sql/execute")
async def execute_sql(
    req: SqlExecuteRequest,
    admin: CurrentUser = Depends(require_admin),
    session: AsyncSession = Depends(get_db_session),
) -> dict:
    """편집된 SQL 을 LLM 없이 직접 실행(검증은 query_db Lambda 가 수행).

    Lambda 의 ValidationError(화이트리스트 위반/DML 등)는 ok:false + error 로
    그대로 반환 — 에디터 UX 의 핵심(분석가가 에러를 보고 고침).
    """
    if not LAMBDA_QUERY_DB:
        raise HTTPException(503, "chat-agent query_db Lambda not configured")
    # 세션 소유 검증(임의 세션 키로 호출 방지)
    sess_row = (
        await session.execute(
            text(
                "SELECT user_id FROM chat_agent.sessions WHERE id = CAST(:sid AS uuid)"
            ).bindparams(sid=str(req.session_id))
        )
    ).first()
    if not sess_row:
        raise HTTPException(404, "Session not found")
    if str(sess_row.user_id) != str(admin.user_id):
        raise HTTPException(403, "Forbidden")

    import boto3

    def _invoke() -> dict:
        client = boto3.client("lambda", region_name=AGENTCORE_REGION)
        resp = client.invoke(
            FunctionName=LAMBDA_QUERY_DB,
            Payload=json.dumps(
                {
                    "sql": req.sql,
                    "session_id": str(req.session_id),
                    "step_id": f"manual-{uuid.uuid4().hex[:8]}",
                }
            ).encode(),
        )
        # ⚠️ invoke 는 핸들러 오류도 HTTP 200 + FunctionError 헤더로 돌려준다 —
        #    그대로 json 을 반환하면 Lambda 장애가 정상 응답으로 보인다(R25-N-C10).
        if resp.get("FunctionError"):
            payload_body = resp["Payload"].read()
            logger.warning(
                "chat sql lambda function_error fn=%s err=%s body=%s",
                LAMBDA_QUERY_DB, resp.get("FunctionError"), payload_body[:500],
            )
            raise _LambdaFunctionError()
        return json.loads(resp["Payload"].read())

    try:
        result = await asyncio.to_thread(_invoke)
    except _LambdaFunctionError:
        raise HTTPException(502, "query_db Lambda returned an error") from None
    except Exception as exc:  # Lambda 호출 자체 실패(권한/네트워크)
        # boto 예외 메시지에는 ARN/리전 등 내부 식별자가 들어간다 — 원문을
        # 클라이언트에 흘리지 않고 로그에만 남긴다.
        logger.warning("chat sql lambda invoke failed: %s", exc)
        raise HTTPException(502, "query execution failed") from exc
    # ok:false(검증 실패 등)도 200 으로 그대로 — 에디터가 에러 메시지 표시
    return result


# ─── Report download (presigned URL) ───
# report_specialist 가 생성한 리포트 파일(S3 reports/ prefix)을 presigned URL 로
# 다운로드. URL 을 agent/스트림에 미리 굽지 않고 클릭 시점에 발급 — 만료(5분) 통제
# + 검증 우회 방지. 보안 2중: (1) 버킷==staging 버킷 AND key prefix=='reports/'
# 화이트리스트(임의 객체 다운로드 차단), (2) require_admin(API 전체 관리자 전용).
REPORT_STAGING_BUCKET = os.environ.get("CHAT_STAGING_BUCKET", "")
REPORT_PRESIGN_EXPIRY = int(os.environ.get("REPORT_PRESIGN_EXPIRY_SECONDS", "300"))
_REPORT_PREFIX = "reports/"
_CONTENT_TYPES = {
    "pdf": "application/pdf",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}


def _parse_report_uri(uri: str) -> tuple[str, str]:
    """s3://bucket/key 를 (bucket, key) 로. 검증 실패 시 400.

    보안 화이트리스트: 버킷은 staging 버킷과 정확히 일치해야 하고, key 는
    'reports/' prefix 로 시작해야 한다. '..' 세그먼트(이중 인코딩 포함 단일
    디코드 후)도 차단.
    """
    if not uri.startswith("s3://"):
        raise HTTPException(400, "Invalid S3 URI")
    rest = uri[len("s3://"):]
    bucket, _, key = rest.partition("/")
    if not bucket or not key:
        raise HTTPException(400, "Malformed S3 URI")
    if not REPORT_STAGING_BUCKET or bucket != REPORT_STAGING_BUCKET:
        raise HTTPException(403, "Bucket not allowed")
    if not key.startswith(_REPORT_PREFIX):
        raise HTTPException(403, "Key prefix not allowed")
    if ".." in key:
        raise HTTPException(400, "Invalid key")
    return bucket, key


@router.get("/reports/download")
async def download_report(
    uri: str,
    admin: CurrentUser = Depends(require_admin),
) -> dict:
    """리포트 파일 presigned 다운로드 URL 발급(5분 만료).

    require_admin 으로 비관리자 접근 차단 + prefix/버킷 화이트리스트로 임의 S3
    객체 다운로드 차단. URL 은 호출 시점에만 발급해 만료를 통제한다.
    """
    import boto3
    from botocore.exceptions import ClientError

    bucket, key = _parse_report_uri(uri)
    file_name = os.path.basename(key) or "report"
    ext = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
    content_type = _CONTENT_TYPES.get(ext, "application/octet-stream")

    s3 = boto3.client("s3", region_name=AGENTCORE_REGION)
    # 존재/권한 확인 — AccessDenied 와 NotFound 를 구분(blanket 404 금지).
    try:
        s3.head_object(Bucket=bucket, Key=key)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("404", "NoSuchKey", "NotFound"):
            raise HTTPException(404, "Report not found") from e
        if code in ("403", "AccessDenied"):
            # presign 권한 자체 문제 — 운영 진단을 위해 구분(IAM s3:GetObject/kms).
            raise HTTPException(502, "Storage access denied (check IAM)") from e
        raise HTTPException(502, "Storage error") from e

    # RFC 5987 — 다국어/특수문자 파일명 안전(헤더 인젝션 방지 + 깨짐 방지).
    from urllib.parse import quote

    disposition = f"attachment; filename*=UTF-8''{quote(file_name)}"
    url = s3.generate_presigned_url(
        "get_object",
        Params={
            "Bucket": bucket,
            "Key": key,
            "ResponseContentDisposition": disposition,
            "ResponseContentType": content_type,
        },
        ExpiresIn=REPORT_PRESIGN_EXPIRY,
    )
    return {"download_url": url, "file_name": file_name, "expires_in": REPORT_PRESIGN_EXPIRY}


