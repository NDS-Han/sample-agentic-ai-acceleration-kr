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
-- KEYS[5] = budget:pendingidx:{scope}:{<scope_id>}       -- 마커 인덱스 해시(선택)
-- ARGV[1] = actual cost (USD, string decimal)
-- ARGV[2] = fallback config JSON ('' = 없음)              -- user D-cap 합성용
-- ARGV[3] = index field ('' = 인덱스 미사용 — 구버전 호출자)
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
local usage_lost = false

-- 카운터 유실 감지(R22 r1 M1): 예약 마커는 살아 있는데 usage 키가 없다는 건
-- 카운터가 축출/failover 로 사라진 것 — 예약치도 그 키와 함께 사라졌으므로
-- (실비 - 예약) 델타는 예약치만큼 과소 계상한다. 유실 상태에서는 실비 전액을
-- 적용하고 센티넬(usage_key..':lost', 같은 hash tag)을 세워 다음 check 가
-- rebuild 하게 한다. 센티넬이 없으면 양수 델타가 키를 재생성해 "키 있음"으로
-- 보여 rebuild 트리거가 영구히 발화하지 않는다.
if redis.call('EXISTS', usage_key) == 0 then
    delta = actual
    usage_lost = true
    redis.call('SET', usage_key .. ':lost', '1', 'EX', 3888000)
end

redis.call('INCRBYFLOAT', usage_key, delta)
redis.call('EXPIRE', usage_key, 3888000)  -- 45d (budget_deduct.lua 와 동일)
redis.call('INCRBYFLOAT', KEYS[4], -reserved)
redis.call('EXPIRE', KEYS[4], 3888000)
redis.call('DEL', marker_key)
if KEYS[5] and ARGV[3] and ARGV[3] ~= '' then
    redis.call('HDEL', KEYS[5], ARGV[3])  -- 인덱스는 베스트에포트 — 마커가 진실
end

-- 논리적 이전값 = 현재 카운터에서 이 요청의 예약분을 뺀 값.
-- 단 유실 경로(usage_lost)에서는 예약치가 카운터와 함께 사라졌으므로
-- used 에서 빼면 안 된다 — used=0 일 때 logical_old=-reserved 가 돼
-- new_used 가 실비보다 예약치만큼 낮게 나오고 임계 교차 알림이 빠진다
-- (R22 r5 N2). 유실 시 논리적 이전값은 0 이다.
local logical_old = usage_lost and 0 or (used - reserved)
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
