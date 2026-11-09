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
    return "normal", 0.0


def _anthropic_events(chunks: int) -> list[dict]:
    """실제 Bedrock(Anthropic) 응답과 같은 이벤트 시퀀스."""
    events = [
        {"type": "message_start", "message": {"id": f"msg_{uuid.uuid4().hex[:12]}",
         "type": "message", "role": "assistant", "content": [], "model": "mock",
         "usage": {"input_tokens": 100, "output_tokens": 1}}},
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
                    converse: bool = False):
    """eventstream 바이트를 흘려보내는 async generator.

    think/hold 구간은 바이트 없이 기다린다 — 게이트웨이의 executor 스레드가
    blocking next() 에 묶이는 상태를 그대로 만든다.
    """
    total = len(events)
    interval = max(0.0, (hold / total) if hold > 0 else INTERVAL)
    for i, ev in enumerate(events):
        if error and i == 3:
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
        if interval:
            await asyncio.sleep(interval)


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
    events = _converse_events(CHUNKS) if converse else _anthropic_events(CHUNKS)
    return StreamingResponse(
        _sse_body(events,
                  think=param if scenario == "think" else 0.0,
                  hold=param if scenario == "hold" else 0.0,
                  error=(scenario == "error"),
                  converse=converse),
        media_type="application/vnd.amazon.eventstream",
        headers={"x-amzn-requestid": req_id},
    )


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
