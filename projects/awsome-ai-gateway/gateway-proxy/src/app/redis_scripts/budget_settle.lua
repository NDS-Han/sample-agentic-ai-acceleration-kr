-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Settle — 예약된 월예산을 실비로 정산 (budget_reserve.lua 의 짝)
--
-- 예약은 사용 카운터에 추정치를 미리 올려 둔 상태다. 완료 시점에 이 스크립트가
-- (실비 - 예약치) 델타를 원자적으로 적용하고 마커를 지운다.
--   · actual = 실비     → 정상 정산
--   · actual = 0        → 전액 환불 (거절/에러/zero-usage 경로)
--
-- 멱등성: 마커가 없으면(이미 정산됐거나 TTL 만료) 아무것도 하지 않는다.
-- finalize 와 release_reservations 가 경쟁적으로 둘 다 호출돼도 한 번만 적용.
--
-- 임계값 평가: 예약 스파이크는 알림을 발화시키지 않아야 하므로, 교차 판정은
-- "예약을 뺀 논리적 이전값 → 실비 반영값"으로 계산한다. (예: used=70,
-- 예약 +40 → 110, 실비 +15 → 정산 후 85. 물리값 110→85 로는 교차가 안 보이지만
-- 논리값 70→85 로는 80% 교차가 정상 발화한다.) 동시 진행 중인 다른 요청의
-- 예약이 논리적 기저에 섞여 교차가 약간 일찍 발화할 수 있다 — 보수적 방향.
--
-- KEYS[1] = budget:{scope}:{<scope_id>}:<period>         -- usage counter
-- KEYS[2] = budget:config:{scope}:{<scope_id>}           -- config (same hash tag)
-- KEYS[3] = budget:pending:{scope}:{<scope_id>}:<req_id> -- 예약 마커
-- KEYS[4] = budget:resvsum:{scope}:{<scope_id>}:{period} -- 미정산 예약 합계
--           (마커 TTL 로 settle 이 no-op 되면 잔류 — 표시가 과소로 기는 방향)
-- ARGV[1] = actual cost (USD, string decimal)
-- ARGV[2] = fallback config JSON ('' = 없음)              -- user D-cap 합성용
--
-- Returns: JSON {settled, new_used, remaining, threshold_triggered, app_clients}

local usage_key  = KEYS[1]
local config_key = KEYS[2]
local marker_key = KEYS[3]
local actual = tonumber(ARGV[1])

local reserved_raw = redis.call('GET', marker_key)
if not reserved_raw then
    return cjson.encode({settled = false})
end
local reserved = tonumber(reserved_raw) or 0

local config_raw = redis.call('GET', config_key)
if not config_raw and ARGV[2] and ARGV[2] ~= '' then
    config_raw = ARGV[2]
end
local limit = 0
local thresholds = {80, 90, 100}
local app_clients = {}
if config_raw then
    local ok, config = pcall(cjson.decode, config_raw)
    if ok then
        limit = tonumber(config.limit_usd) or 0
        if config.thresholds then
            thresholds = config.thresholds
        end
        if config.app_clients then
            app_clients = config.app_clients
        end
    end
end

local used = tonumber(redis.call('GET', usage_key) or '0')
local delta = actual - reserved

redis.call('INCRBYFLOAT', usage_key, delta)
redis.call('EXPIRE', usage_key, 3888000)  -- 45d (budget_deduct.lua 와 동일)
redis.call('INCRBYFLOAT', KEYS[4], -reserved)
redis.call('EXPIRE', KEYS[4], 3888000)
redis.call('DEL', marker_key)

-- 논리적 이전값 = 현재 카운터에서 이 요청의 예약분을 뺀 값.
local logical_old = used - reserved
local logical_new = logical_old + actual

local triggered = cjson.null
if limit > 0 then
    local old_pct = (logical_old / limit) * 100
    local new_pct = (logical_new / limit) * 100
    local highest = nil
    for _, t in ipairs(thresholds) do
        if old_pct < t and new_pct >= t then
            if highest == nil or t > highest then
                highest = t
            end
        end
    end
    if highest ~= nil then
        triggered = highest
    end
end

return cjson.encode({
    settled = true,
    new_used = logical_new,
    remaining = limit - logical_new,
    threshold_triggered = triggered,
    app_clients = app_clients
})
