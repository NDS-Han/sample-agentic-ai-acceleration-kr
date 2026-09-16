"""H: 검색 결과 항목별 본문 자르기 · O: 한 턴의 검색 동시 실행.

2026-09-16 US 실측. (H) max_result_chars 는 JSON 을 통째로 잘라 뒤쪽 결과가 레코드 중간에서
사라지고 앞쪽 본문은 수천 자가 그대로 실렸다(원본 13~24k자). 항목별로 자르면 결과 5개의
제목·URL·머리 본문이 모두 남고 토큰은 −40~50%. (O) 한 턴의 검색 2개를 차례로 기다리면
fan-out 턴마다 5초 안팎이 쌓였다(검색 1회 4~9초). 동시에 돌려도 비용은 같다.
"""

from __future__ import annotations

import asyncio
import json
import time

import app.services.web_search_loop as wsl

LONG = ("문장 하나. " * 400).strip()          # ~2,400 chars


def _raw(items):
    return json.dumps({"results": items}, ensure_ascii=False)


def test_trim_keeps_every_result_cuts_each_body_and_drops_url_duplicates():
    raw = _raw([
        {"url": "https://a.com/x", "title": "A", "text": LONG, "publishedDate": "2026-09-01"},
        {"url": "https://a.com/x/", "title": "A dup", "text": "dup"},
        {"url": "https://b.com", "title": "B", "text": "  짧은   본문\n\n입니다.  "},
        {"url": "https://c.com", "title": "C", "text": LONG},
    ])
    out, changed = wsl._trim_results(raw, 1500)
    assert changed
    items = json.loads(out)["results"]
    assert [i["title"] for i in items] == ["A", "B", "C"], "URL 중복만 빠지고 순서 유지"
    assert items[0]["publishedDate"] == "2026-09-01", "본문 외 키는 보존"
    assert len(items[0]["text"]) <= 1500 + 2 and items[0]["text"].endswith(" …")
    assert items[0]["text"].rstrip(" …").endswith("."), "문장 경계에서 자른다"
    assert items[1]["text"] == "짧은 본문 입니다.", "공백 정리, 짧은 본문은 그대로"
    assert len(out) < len(raw) * 0.6, (len(out), len(raw))


def test_trim_is_a_no_op_when_disabled_or_not_json():
    raw = _raw([{"url": "https://a.com", "text": LONG}])
    assert wsl._trim_results(raw, 0) == (raw, False)
    assert wsl._trim_results("not json", 100) == ("not json", False)
    assert wsl._trim_results('{"error":"x"}', 100) == ('{"error":"x"}', False)


async def test_do_search_trims_then_caps_and_keeps_the_trace():
    class _Mcp:
        async def ensure_initialized(self):
            return "WebSearch"

        async def search(self, q, n):
            class _R:
                raw_text = _raw([{"url": "https://a.com", "title": "t", "text": LONG}] * 1)
                results = [{"url": "https://a.com", "title": "t"}]
            return _R()

    text, ok, trace = await wsl._do_search(_Mcp(), {"query": "q"}, 5, 0, 300)
    assert ok and len(json.loads(text)["results"][0]["text"]) <= 302
    assert trace.startswith(wsl._TRACE_PREFIX) and "a.com" in trace


async def test_a_turns_searches_run_concurrently_and_keep_order():
    class _Mcp:
        def __init__(self):
            self.calls = []

        async def ensure_initialized(self):
            return "WebSearch"

        async def search(self, q, n):
            self.calls.append(q)
            await asyncio.sleep(0.25)

            class _R:
                raw_text = _raw([{"url": f"https://{q}.com", "title": q, "text": q}])
            return _R()

    mcp = _Mcp()
    t0 = time.monotonic()
    out = await wsl._run_turn_searches(
        mcp, [{"query": "q1"}, {"query": "q2"}, {"query": "q3"}], 2,
        time.monotonic() + 60, 5, 0, 0)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.45, f"두 검색이 순차(0.5s+)로 돌았다: {elapsed:.2f}s"
    assert [r[3] for r in out] == [None, None, "capped"]
    assert "q1" in out[0][0] and "q2" in out[1][0], "결과는 입력 순서대로"
    assert sorted(mcp.calls) == ["q1", "q2"], "상한을 넘긴 검색은 실행하지 않는다"
    assert out[2][2].endswith("skipped (per-turn limit)")


async def test_deadline_already_passed_skips_all_without_calling():
    class _Mcp:
        calls = 0

        async def search(self, q, n):
            _Mcp.calls += 1

    out = await wsl._run_turn_searches(_Mcp(), [{"query": "q"}], 5, time.monotonic() - 1, 5, 0, 0)
    assert out[0][3] == "deadline" and _Mcp.calls == 0
    blk = wsl._anthropic_tool_result("t", *out[0][:2], out[0][3])
    assert blk == {"type": "tool_result", "tool_use_id": "t",
                   "content": "web search deadline exceeded", "is_error": True}
    item = wsl._responses_call_output("c", "", False, "capped")
    assert json.loads(item["output"])["error"].startswith("per-turn web search limit")
