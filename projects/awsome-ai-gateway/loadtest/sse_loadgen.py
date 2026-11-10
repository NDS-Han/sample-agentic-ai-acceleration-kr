"""SSE-hold 부하 발생기 — asyncio + httpx (k6 대안, 의존성 없음).

각 태스크가 SSE 스트림 하나를 연다 = 동시 스트림 N개 = 태스크 N개.
mock 모델 별칭으로 시나리오를 섞는다.

실행:
  python sse_loadgen.py --url https://gw.example.com --key vk-... \
      --conns 200 --model mock-normal,mock-think-30 --hold 60

측정: TTFB(큐 대기 신호), 완료/에러율, p50/p95 지연분위.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import time
from collections import Counter

import httpx

BODY_TEMPLATE = {
    "max_tokens": 512,
    "messages": [{"role": "user", "content": "loadtest"}],
    "stream": True,
}


async def hold_one(client: httpx.AsyncClient, url: str, model: str,
                   hold_s: float, results: list, stats: Counter):
    """SSE 하나를 열고 hold_s 동안 유지한다."""
    t0 = time.monotonic()
    ttfb = None
    chunks = 0
    try:
        async with client.stream(
            "POST", f"{url}/v1/messages",
            json={**BODY_TEMPLATE, "model": model},
            headers={"anthropic-version": "2023-06-01"},
        ) as r:
            ttfb = time.monotonic() - t0
            if r.status_code != 200:
                stats[f"http_{r.status_code}"] += 1
                results.append((model, ttfb, 0, f"http_{r.status_code}"))
                return
            deadline = t0 + hold_s
            async for line in r.aiter_lines():
                chunks += 1
                if time.monotonic() > deadline:
                    break
            dur = time.monotonic() - t0
            stats["ok"] += 1
            results.append((model, ttfb, dur, "ok"))
    except Exception as e:
        stats[type(e).__name__] += 1
        results.append((model, ttfb, time.monotonic() - t0, type(e).__name__))


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--conns", type=int, default=50)
    ap.add_argument("--model", default="mock-normal")
    ap.add_argument("--hold", type=float, default=30.0,
                    help="VU 당 스트림 유지 초 (읽기 강제 중단 — disconnect 경로)")
    ap.add_argument("--ramp", type=float, default=5.0,
                    help="conns 를 이 시간 동안 점진적으로 올린다")
    args = ap.parse_args()

    models = args.model.split(",")
    stats: Counter = Counter()
    results: list = []

    limits = httpx.Limits(max_connections=args.conns + 50,
                          max_keepalive_connections=args.conns + 50)
    timeout = httpx.Timeout(connect=15.0, read=None, write=30.0, pool=120.0)
    async with httpx.AsyncClient(
        headers={"x-api-key": args.key},
        limits=limits, timeout=timeout, verify=True,
    ) as client:
        tasks = []
        gap = args.ramp / max(1, args.conns)
        for i in range(args.conns):
            m = models[i % len(models)]
            tasks.append(asyncio.create_task(
                hold_one(client, args.url, m, args.hold, results, stats)))
            await asyncio.sleep(gap)
        await asyncio.gather(*tasks)

    ttfbs = sorted(r[1] for r in results if r[1] is not None)
    def pct(v, p):
        return v[min(len(v) - 1, int(len(v) * p))] if v else 0

    print(f"\n=== {args.conns} conns × models={models} hold={args.hold}s ===")
    print(f"outcomes: {dict(stats)}")
    if ttfbs:
        print(f"TTFB: p50={pct(ttfbs,.5):.2f}s p95={pct(ttfbs,.95):.2f}s "
              f"max={ttfbs[-1]:.2f}s mean={statistics.mean(ttfbs):.2f}s")


if __name__ == "__main__":
    asyncio.run(main())
