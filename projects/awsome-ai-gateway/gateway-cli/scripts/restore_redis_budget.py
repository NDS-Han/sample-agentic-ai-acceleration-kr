#!/usr/bin/env python3
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""
Redis Budget & Daily Usage 복구 스크립트

Redis가 재시작되거나 LRU로 키가 삭제됐을 때
DB(usage_logs, budget_usages)에서 복구합니다.

Usage:
  python3 restore_redis_budget.py
  python3 restore_redis_budget.py --user-id <uuid>
  python3 restore_redis_budget.py --date 2026-04-11
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone, date

import asyncpg
import redis.asyncio as aioredis

# KST = UTC+9 고정 오프셋. 이 스크립트는 standalone 실행이라 gateway-proxy 의
# app.periods 를 import 할 수 없으므로 같은 상수를 둔다(두 곳을 함께 바꿀 것).
KST = timezone(timedelta(hours=9))


# ── 환경 변수 ──────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql://gateway:gateway_dev_password@localhost:5432/gateway",
)
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")


async def restore(
    user_id: str | None = None,
    target_date: str | None = None,
    dry_run: bool = False,
) -> None:
    # ⚠️ **KST** 경계 — 이 스크립트가 되살리는 키를 gateway-proxy 가 KST 로 읽고 쓴다
    # (gateway-proxy/src/app/periods.py). UTC 로 잡으면 KST 09:00 이전에 실행할 때
    # 하루 전 키를 복구해, 정작 조회되는 오늘 키는 비어 있는 채로 남는다.
    period = target_date[:7] if target_date else datetime.now(KST).strftime("%Y-%m")
    today = target_date or datetime.now(KST).strftime("%Y-%m-%d")

    # DB + Redis 연결
    db_url = DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    conn = await asyncpg.connect(db_url)
    r = await aioredis.from_url(REDIS_URL, decode_responses=True)

    print(f"=== Redis Budget & Daily Usage 복구 ===")
    print(f"Period : {period}")
    print(f"Date   : {today}")
    print(f"User   : {user_id or 'ALL'}")
    print(f"DryRun : {dry_run}")
    print()

    # 복구 대상 사용자 목록
    if user_id:
        users = [{"id": user_id}]
    else:
        rows = await conn.fetch("SELECT id FROM auth.users WHERE is_active = true")
        users = [{"id": str(r["id"])} for r in rows]

    restored_count = 0

    for u in users:
        uid = u["id"]

        # ── 1. 월별 budget 카운터 복구 ─────────────────────────────────
        # Redis Cluster hash tag on {uid}: must match gateway-proxy key format.
        budget_key = f"budget:user:{{{uid}}}:{period}"
        existing = await r.get(budget_key)

        # budget_usages 우선, fallback: usage_logs SUM
        row = await conn.fetchrow(
            "SELECT used_usd FROM budget.budget_usages "
            "WHERE scope = 'USER' AND scope_id = $1::uuid AND period = $2",
            uid, period,
        )
        if row:
            used_from_db = float(row["used_usd"])
        else:
            row2 = await conn.fetchrow(
                "SELECT COALESCE(SUM(cost_usd), 0) AS total FROM usage.usage_logs "
                "WHERE user_id = $1::uuid AND to_char(requested_at, 'YYYY-MM') = $2",
                uid, period,
            )
            used_from_db = float(row2["total"]) if row2 else 0.0

        if used_from_db > 0:
            if existing:
                existing_val = float(existing)
                if abs(existing_val - used_from_db) < 0.001:
                    print(f"[{uid[:8]}] budget OK: ${existing_val:.4f} (no change needed)")
                else:
                    print(f"[{uid[:8]}] budget MISMATCH: Redis=${existing_val:.4f} DB=${used_from_db:.4f} → restoring from DB")
                    if not dry_run:
                        await r.set(budget_key, str(used_from_db))
                        restored_count += 1
            else:
                print(f"[{uid[:8]}] budget MISSING → restoring ${used_from_db:.4f}")
                if not dry_run:
                    await r.set(budget_key, str(used_from_db))
                    restored_count += 1

        # ── 2. 일별 모델별 카운터 복구 ────────────────────────────────
        daily_prefix = f"usage:daily:user:{{{uid}}}:{today}"
        models_key = f"{daily_prefix}:models"
        existing_models = await r.smembers(models_key)

        rows = await conn.fetch(
            """
            SELECT
              model_alias,
              COALESCE(SUM(cost_usd), 0)              AS cost,
              COALESCE(SUM(input_tokens), 0)           AS input,
              COALESCE(SUM(output_tokens), 0)          AS output,
              COALESCE(SUM(cache_creation_tokens), 0)  AS cache_write,
              COALESCE(SUM(cache_read_tokens), 0)      AS cache_read,
              COUNT(*)                                  AS requests
            FROM usage.usage_logs
            -- ⚠️ DATE(requested_at) 는 **DB 세션 TZ**(pod 은 UTC)로 자른다. 일별
            -- 카운터는 KST 일자 키이므로(daily_aggregator.py:43 과 같은 규칙)
            -- 명시적으로 KST 로 변환해야 복구값이 키와 같은 구간을 담는다.
            WHERE user_id = $1::uuid
              AND DATE(requested_at AT TIME ZONE 'Asia/Seoul') = $2::date
            GROUP BY model_alias
            """,
            uid, date.fromisoformat(today),
        )

        total_cost = 0.0
        total_tokens = 0

        for row in rows:
            alias = row["model_alias"]
            mp = f"{daily_prefix}:model:{alias}"

            total_cost += float(row["cost"])
            total_tokens += int(row["input"]) + int(row["output"])

            missing = alias not in existing_models
            label = "MISSING" if missing else "CHECK"
            print(f"  [{uid[:8]}] {label} {alias}: "
                  f"${float(row['cost']):.4f} "
                  f"in={row['input']} cw={row['cache_write']} cr={row['cache_read']} out={row['output']}")

            if not dry_run:
                pipe = r.pipeline()
                pipe.set(f"{mp}:cost", str(float(row["cost"])))
                pipe.set(f"{mp}:input", str(int(row["input"])))
                pipe.set(f"{mp}:output", str(int(row["output"])))
                pipe.set(f"{mp}:cache_write", str(int(row["cache_write"])))
                pipe.set(f"{mp}:cache_read", str(int(row["cache_read"])))
                pipe.set(f"{mp}:requests", str(int(row["requests"])))
                pipe.expire(f"{mp}:cost", 172800)
                pipe.expire(f"{mp}:input", 172800)
                pipe.expire(f"{mp}:output", 172800)
                pipe.expire(f"{mp}:cache_write", 172800)
                pipe.expire(f"{mp}:cache_read", 172800)
                pipe.expire(f"{mp}:requests", 172800)
                await pipe.execute()
                await r.sadd(models_key, alias)
                await r.expire(models_key, 172800)
                restored_count += 1

        if total_cost > 0 and not dry_run:
            await r.set(f"{daily_prefix}:cost", str(total_cost))
            await r.set(f"{daily_prefix}:tokens", str(total_tokens))
            await r.expire(f"{daily_prefix}:cost", 172800)
            await r.expire(f"{daily_prefix}:tokens", 172800)

    await conn.close()
    await r.aclose()

    print()
    if dry_run:
        print(f"[DRY RUN] Would restore {restored_count} Redis keys.")
    else:
        print(f"✓ Restored {restored_count} Redis keys.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Redis Budget & Daily Usage 복구")
    parser.add_argument("--user-id", help="특정 사용자만 복구 (UUID)")
    parser.add_argument("--date", help="복구 대상 날짜 YYYY-MM-DD (기본: 오늘)")
    parser.add_argument("--dry-run", action="store_true", help="변경 없이 미리보기")
    args = parser.parse_args()

    asyncio.run(restore(
        user_id=args.user_id,
        target_date=args.date,
        dry_run=args.dry_run,
    ))


if __name__ == "__main__":
    main()