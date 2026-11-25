"""Mock Bedrock Runtime — 부하테스트 전용 (실 AWS 호출·비용 $0).

gateway-proxy 는 `AWS_ENDPOINT_URL_BEDROCK_RUNTIME=http://mock-bedrock...`
env 로 이 서버를 향하게 된다 (botocore 네이티브 env 오버라이드 — 게이트웨이
코드 변경 없음). boto3 스레드/소켓/eventstream 파싱 경로는 실제와 동일하게
돌고, SigV4 서명도 그대로 수행된다 (mock 은 서명을 무시한다).

시나리오는 URL 의 model_id 에 인코딩한다 (요청 바디를 건드리지 않으므로
게이트웨이 검증을 그대로 통과):

  POST /model/<model>/invoke-with-response-stream   → eventstream SSE
  POST /model/<model>/invoke                        → 단건 JSON
  POST /model/<model>/converse-stream               → converse 유니온 SSE

  mock-normal        기본 시나리오 (env 값: CHUNKS/INTERVAL)
  mock-think-120     첫 청크 후 120s 무응답 — thinking 공백 동안 executor
                     스레드 점유 재현 (★ starvation 시나리오)
  mock-hold-300      스트림을 총 300s 유지 (수명 분산 테스트)
  mock-error         3청크 후 internalServerException 프레임
  mock-429           즉시 429 throttlingException (invoke 실패)
  mock-ws            웹 검색 턴 시뮬레이션(wave 3 proof): 대화에 tool_result 가 없으면
                     web_search tool_use 를 내고(stop_reason=tool_use), 있으면 일반 답을 낸다

wave 3 비용 대조용:
  GET  /ledger       스트림별 실제 발행 기록 — request_id, scenario, input, 발행한 출력 델타 수,
                     최종 usage(message_delta) 발행 여부, 오류 여부. POST /ledger/reset 으로 비운다.
  POST /mcp          AgentCore Gateway MCP 스텁(initialize / tools/list / tools/call WebSearch)
                     — tools/call 횟수를 /ledger 의 mcp_calls 로 센다.

env knob (모든 시나리오의 기본 파라미터):
  MOCK_CHUNKS    발송할 content_block_delta 수   (기본 50)
  MOCK_INTERVAL  청크 간격 초                     (기본 0.05)
  MOCK_TEXT      청크당 텍스트                     (기본 짧은 문장)
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

from eventstream import chunk_event, converse_event, exception_event

app = FastAPI(title="Mock Bedrock Runtime")

CHUNKS = int(os.environ.get("MOCK_CHUNKS", "50"))
INTERVAL = float(os.environ.get("MOCK_INTERVAL", "0.05"))
TEXT = os.environ.get("MOCK_TEXT", "mock chunk ")

#: 스트림별 발행 기록(wave 3 proof) — 프로세스 메모리.
LEDGER: list[dict] = []
MCP_CALLS = 0
INPUT_TOKENS = 100

_stream_re = re.compile(r"^(?P<model>.+?)/(?P<ep>invoke-with-response-stream|converse-stream|invoke)$")
_think_re = re.compile(r"mock-think-(\d+)")
_hold_re = re.compile(r"mock-hold-(\d+)")


def _scenario(model: str) -> tuple[str, float]:
    """model_id → (시나리오, 파라미터)."""
    if m := _think_re.search(model):
        return "think", float(m.group(1))
    if m := _hold_re.search(model):
        return "hold", float(m.group(1))
    if "mock-error" in model:
        return "error", 0.0
    if "mock-429" in model:
        return "429", 0.0
    if "mock-ws" in model:
        return "ws", 0.0
    return "normal", 0.0


def _anthropic_events(chunks: int) -> list[dict]:
    """실제 Bedrock(Anthropic) 응답과 같은 이벤트 시퀀스."""
    events = [
        {"type": "message_start", "message": {"id": f"msg_{uuid.uuid4().hex[:12]}",
         "type": "message", "role": "assistant", "content": [], "model": "mock",
         "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
    ]
    events += [
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": TEXT}}
        for _ in range(chunks)
    ]
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": chunks}},
        {"type": "message_stop"},
    ]
    return events


def _ws_search_events() -> list[dict]:
    """웹 검색 턴 1 — 게이트웨이가 주입한 web_search 도구를 부른다. 델타 5개 = 출력 5 토큰."""
    parts = ['{"query":"', " the", " the", " the", '"}']
    events = [
        {"type": "message_start", "message": {"id": f"msg_{uuid.uuid4().hex[:12]}",
         "type": "message", "role": "assistant", "content": [], "model": "mock",
         "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": 1}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:12]}",
                           "name": "web_search", "input": {}}},
    ]
    events += [{"type": "content_block_delta", "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": p}} for p in parts]
    events += [
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"},
         "usage": {"output_tokens": len(parts)}},
        {"type": "message_stop"},
    ]
    return events


def _has_tool_result(body: bytes) -> bool:
    try:
        req = json.loads(body or b"{}")
    except ValueError:
        return False
    for m in req.get("messages") or []:
        c = m.get("content")
        if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                       for b in c):
            return True
    return False


def _converse_events(chunks: int) -> list[dict]:
    """converse-stream 유니온 이벤트 (멤버명이 camelCase 유니온)."""
    events = [{"messageStart": {"role": "assistant"}},
              {"contentBlockStart": {"start": {"text": ""}, "contentBlockIndex": 0}}]
    events += [{"contentBlockDelta": {"delta": {"text": TEXT}, "contentBlockIndex": 0}}
               for _ in range(chunks)]
    events += [{"contentBlockStop": {"contentBlockIndex": 0}},
               {"messageStop": {"stopReason": "end_turn"}},
               {"metadata": {"usage": {"inputTokens": 100, "outputTokens": chunks}}}]
    return events


async def _sse_body(events: list[dict], *, think: float = 0.0,
                    hold: float = 0.0, error: bool = False,
                    converse: bool = False, rec: dict | None = None):
    """eventstream 바이트를 흘려보내는 async generator.

    think/hold 구간은 바이트 없이 기다린다 — 게이트웨이의 executor 스레드가
    blocking next() 에 묶이는 상태를 그대로 만든다. ``rec`` 에 실제로 발행한 것을 남긴다
    (출력 델타 수·최종 usage 발행 여부 — wave 3 비용 대조의 기준값).
    """
    total = len(events)
    interval = max(0.0, (hold / total) if hold > 0 else INTERVAL)
    try:
        for i, ev in enumerate(events):
            if error and i == 3:
                if rec is not None:
                    rec["error"] = True
                yield exception_event("internalServerException",
                                      "mock injected mid-stream error")
                return
            if think > 0 and i == 2:
                await asyncio.sleep(think)
            if converse:
                member = next(iter(ev))
                yield converse_event(member, ev[member])
            else:
                yield chunk_event(ev)
            if rec is not None:
                et = ev.get("type")
                if et == "content_block_delta":
                    rec["deltas"] += 1
                elif et == "message_delta":
                    rec["final_output"] = (ev.get("usage") or {}).get("output_tokens")
                elif et == "message_stop":
                    rec["completed"] = True
            if interval:
                await asyncio.sleep(interval)
    finally:
        if rec is not None:
            rec["t_end"] = time.time()


@app.post("/model/{rest:path}")
async def model_invoke(rest: str, request: Request):
    m = _stream_re.match(rest)
    if not m:
        return JSONResponse({"message": "unknown path"}, status_code=404)
    model, ep = m.group("model"), m.group("ep")
    scenario, param = _scenario(model)
    req_id = uuid.uuid4().hex

    if scenario == "429":
        return JSONResponse(
            {"message": "mock throttling"}, status_code=429,
            headers={"x-amzn-errortype": "throttlingException",
                     "x-amzn-requestid": req_id},
        )

    if ep == "invoke":
        # non-streaming — Anthropic 단건 응답 (think/hold 지연은 시뮬레이트)
        if scenario == "error":
            return JSONResponse({"message": "mock internal error"},
                                status_code=500,
                                headers={"x-amzn-requestid": req_id})
        if scenario == "think" or scenario == "hold":
            await asyncio.sleep(param)
        return JSONResponse(
            {"id": f"msg_{req_id[:12]}", "type": "message", "role": "assistant",
             "content": [{"type": "text", "text": TEXT * CHUNKS}],
             "model": model, "stop_reason": "end_turn",
             "usage": {"input_tokens": 100, "output_tokens": CHUNKS}},
            headers={"x-amzn-requestid": req_id},
        )

    converse = ep == "converse-stream"
    if scenario == "ws" and not converse:
        body = await request.body()
        turn = "answer" if _has_tool_result(body) else "search"
        events = _anthropic_events(CHUNKS) if turn == "answer" else _ws_search_events()
    else:
        turn = None
        events = _converse_events(CHUNKS) if converse else _anthropic_events(CHUNKS)
    rec = {"request_id": req_id, "model": model, "scenario": scenario, "turn": turn,
           "input": INPUT_TOKENS, "deltas": 0, "final_output": None, "completed": False,
           "error": False, "t_start": time.time()}
    LEDGER.append(rec)
    return StreamingResponse(
        _sse_body(events,
                  think=param if scenario == "think" else 0.0,
                  hold=param if scenario == "hold" else 0.0,
                  error=(scenario == "error"),
                  converse=converse, rec=rec),
        media_type="application/vnd.amazon.eventstream",
        headers={"x-amzn-requestid": req_id},
    )


@app.get("/ledger")
async def ledger():
    return {"streams": LEDGER, "mcp_calls": MCP_CALLS}


@app.post("/ledger/reset")
async def ledger_reset():
    global MCP_CALLS
    LEDGER.clear()
    MCP_CALLS = 0
    return {"ok": True}


@app.post("/mcp")
async def mcp(request: Request):
    """AgentCore Gateway MCP 스텁 — 게이트웨이의 AgentCoreMcpClient 가 쓰는 세 메서드만."""
    global MCP_CALLS
    msg = await request.json()
    method, mid = msg.get("method"), msg.get("id")
    if method == "notifications/initialized":
        return JSONResponse(None, status_code=202)
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "mock-agentcore", "version": "1"}}
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": result},
                            headers={"Mcp-Session-Id": uuid.uuid4().hex})
    if method == "tools/list":
        tool = {"name": "web-search-tool___WebSearch", "description": "mock web search",
                "inputSchema": {"type": "object",
                                "properties": {"query": {"type": "string"},
                                               "maxResults": {"type": "integer"}},
                                "required": ["query"]}}
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {"tools": [tool]}})
    if method == "tools/call":
        MCP_CALLS += 1
        payload = {"results": [{"text": "mock result text", "url": "https://example.invalid/a",
                                "title": "Mock", "publishedDate": "2026-11-01"}]}
        return JSONResponse({"jsonrpc": "2.0", "id": mid, "result": {
            "content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}})
    return JSONResponse({"jsonrpc": "2.0", "id": mid,
                         "error": {"code": -32601, "message": "method not found"}})


@app.get("/health")
async def health():
    return {"ok": True, "chunks": CHUNKS, "interval": INTERVAL}


@app.get("/metrics")
async def metrics():
    # 부하 스크립트용 아주 단순한 카운터 — 시작된 요청/완료된 스트림
    return {"started": _started, "time": time.time()}


_started = 0


@app.middleware("http")
async def count_requests(request: Request, call_next):
    global _started
    if request.method == "POST":
        _started += 1
    return await call_next(request)
