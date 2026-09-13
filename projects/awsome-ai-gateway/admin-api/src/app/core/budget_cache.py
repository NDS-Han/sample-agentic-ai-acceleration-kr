# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``budget:config:user:{uid}`` 캐시 키를 원자적으로 쓴다.

왜 이 모듈이 필요한가
---------------------
그 키에는 두 부류의 필드가 섞여 산다:

* **총액 예산** — ``limit_usd`` / ``policy`` / ``thresholds`` / ``soft_limit_pct`` …
  예산 설정 변경과 CLI 로그인이 쓴다.
* **앱별 하위 한도 게이트** — ``app_clients``.
  per-app 예산을 만들거나 지울 때만 쓴다.

두 부류를 쓰는 **주체가 다르고 시점도 다르다.** 그래서 각자가 페이로드 전체를
GET-modify-SET 하면 서로의 필드를 지운다. 그리고 이 손실은 조용하다:
``budget_check.lua`` 는 ``config.app_clients or {}`` 로 읽어 없으면 빈 테이블을 쓰고,
게이트웨이는 ``isinstance(list)`` 검사에서 떨어져 **앱별 예산 평가를 통째로 건너뛴다**
— 에러도 로그도 없이 한도가 사라진다.

실측된 세 가지 실패
-------------------
1. **로그인 클로버(가장 나쁘다).** ``CLIService._cache_for_gateway`` 가 총액 필드만
   담은 페이로드로 ``redis.set`` 을 했다. ``app_clients`` 가 사라지고, ``ex=`` 도 없어서
   **기존 TTL 까지 날아간다**(Redis SET 은 KEEPTTL 없이는 TTL 을 버린다). 그리고
   게이트웨이는 키가 **없을 때만** 재수화하므로(``if not await redis.exists(...)``),
   present-and-persistent 상태가 된 키는 영구히 그대로다 — 자가치유가 없다.
   CLI 로그인과 OIDC 교환 **매번** 이 경로를 탄다.
2. **``_sync_redis_thresholds`` 의 GET-modify-SET.** ``await redis.get`` 이 이벤트 루프를
   양보하므로 uvicorn 워커 **하나** 안에서도 두 요청이 교차한다.
3. **``_refresh_user_app_clients`` 의 GET-modify-SET.** 이쪽은 새로 추가된 client 를
   싣는 쓰기라, 손실이 곧 예산 우회다(단순 staleness 가 아니다).

해법
----
Redis 서버에서 원자적으로 실행되는 Lua 로 **읽기-병합-쓰기를 한 번에** 한다. Lua 는
Redis 안에서 단일 스레드로 돌므로 두 호출이 겹치지 않는다.

⚠️ ``WATCH``/``MULTI`` 로도 되지만 재시도 루프가 필요하고, 클러스터 모드에서 키가 한
   슬롯에 있어야 한다. Lua + 단일 키가 더 단순하고, 이 레포는 이미 게이트웨이에서
   ``budget_check.lua`` 를 쓰고 있어 같은 방식이다.
"""

from __future__ import annotations

import json

import structlog

logger = structlog.get_logger()

#: 총액 예산 필드를 병합하면서 ``app_clients`` 는 기존 값을 보존한다.
#:
#: 왜 "보존" 이지 "덮어쓰기 금지" 가 아닌가: 호출자가 새 ``app_clients`` 를 들고 있을
#: 때는 그것을 써야 하고(그게 아래 REFRESH 스크립트다), 총액을 쓰는 호출자는 그 필드를
#: 아예 모른다. 그래서 두 스크립트로 나눈다 — 한 스크립트가 두 의도를 다루려 하면
#: "빈 리스트를 넘긴 것" 과 "안 넘긴 것" 을 구분할 수 없다.
_WRITE_TOTAL_LUA = """
local key = KEYS[1]
local payload = cjson.decode(ARGV[1])
local ttl = tonumber(ARGV[2])

local raw = redis.call('GET', key)
if raw then
    local prev = cjson.decode(raw)
    -- ⚠️ nil 검사가 아니라 필드 존재 검사다. `app_clients = []` 는 "활성 per-app 예산이
    --    없다" 는 **의미 있는 값**이고, 키가 아예 없는 것과 다르다. truthiness 로
    --    판정하면 빈 리스트가 사라져 게이트웨이가 DB 에서 재도출하게 되는데, 그 사이
    --    창에서 한도가 적용되지 않는다.
    if prev['app_clients'] ~= nil then
        payload['app_clients'] = prev['app_clients']
    end
end

redis.call('SET', key, cjson.encode(payload), 'EX', ttl)
return 1
"""

#: ``app_clients`` 만 갈아끼우고 나머지는 그대로 둔다. 키가 없으면 **아무것도 하지
#: 않는다**(0 반환) — 총액 필드를 모르는 채로 키를 만들면 게이트웨이가 한도 없는
#: 설정으로 읽는다. 없으면 게이트웨이가 DB 에서 재도출하는 것이 맞다.
_REFRESH_APP_CLIENTS_LUA = """
local key = KEYS[1]
local clients = cjson.decode(ARGV[1])
local ttl = tonumber(ARGV[2])

local raw = redis.call('GET', key)
if not raw then
    return 0
end

local data = cjson.decode(raw)
data['app_clients'] = clients
redis.call('SET', key, cjson.encode(data), 'EX', ttl)
return 1
"""


async def write_user_budget_config(redis, user_id, config_data: dict, ttl: int) -> None:
    """총액 예산 필드를 쓴다. ``app_clients`` 는 기존 값이 있으면 보존한다.

    ⚠️ ``redis.set`` 을 직접 부르지 말 것 — 그게 로그인 클로버의 형태다.

    Redis 가 없거나 스크립트가 실패해도 **예외를 올리지 않는다.** 이 캐시는 게이트웨이가
    미스 시 DB 에서 재도출할 수 있는 파생 데이터이므로, 캐시 쓰기 실패로 로그인이나
    예산 설정을 실패시키는 것은 거래가 성립하지 않는다.
    """
    if redis is None:
        return
    key = f"budget:config:user:{{{user_id}}}"
    try:
        await redis.eval(_WRITE_TOTAL_LUA, 1, key, json.dumps(config_data), str(ttl))
    except Exception:
        # ⚠️ 폴백으로 `redis.set` 을 하지 않는다. 그것이 정확히 없애려는 클로버다 —
        #    "Lua 가 안 되면 덮어쓰자" 는 실패 모드를 다시 들여오는 것이다.
        logger.warning("budget_config_cache_write_failed", user_id=str(user_id))


async def refresh_user_app_clients(redis, user_id, clients: list[str], ttl: int) -> bool:
    """``app_clients`` 만 갈아끼운다. 키가 없으면 ``False``(아무것도 안 함)."""
    if redis is None:
        return False
    key = f"budget:config:user:{{{user_id}}}"
    try:
        res = await redis.eval(_REFRESH_APP_CLIENTS_LUA, 1, key, json.dumps(clients), str(ttl))
        return bool(res)
    except Exception:
        logger.warning("budget_app_clients_refresh_failed", user_id=str(user_id))
        return False
