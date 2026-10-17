-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Deduct — 실제 비용 원자적 차감 + 임계값 교차 체크
-- BR-BDG-05, BR-BDG-06
--
-- KEYS[1] = budget:user:{<user_id>}:<period>   -- hash tag on user_id
-- KEYS[2] = budget:config:user:{<user_id>}     -- same hash tag → same slot
-- ARGV[1] = cost (USD, string decimal)
-- ARGV[2] = fallback config JSON ('' = 없음). user 키가 없을 때 팀 기본 cap D
--           로 threshold 교차를 평가한다 (D-10: D 적용 유저도 임계값 알림).
--
-- Returns: JSON {new_used, remaining, threshold_triggered}

local usage_key = KEYS[1]
local config_key = KEYS[2]
local cost = tonumber(ARGV[1])

local config_raw = redis.call('GET', config_key)
if not config_raw and ARGV[2] and ARGV[2] ~= '' then
    config_raw = ARGV[2]
end
local limit = 0
local thresholds = {80, 90, 100}
local app_clients = {}
if config_raw then
    local config = cjson.decode(config_raw)
    limit = tonumber(config.limit_usd) or 0
    if config.thresholds then
        thresholds = config.thresholds
    end
    if config.app_clients then
        app_clients = config.app_clients
    end
end

local used = tonumber(redis.call('GET', usage_key) or '0')
local new_used = used + cost

-- 차감 실행. period 가 키에 박혀 있어도 TTL 은 없던 상태였다 — 월이 지난 카운터가
-- 영구히 남아 Redis 를 서서히 채운다. 마지막 쓰기로부터 45 일로 잡으면 해당 월
-- 카운터가 다음 달 초의 조회(usage/me)까지 살아남고 그 뒤엔 정리된다.
-- 카운터 유실 감지(R22 r1 M1): 첫 쓰기가 키를 생성하면 센티넬을 세워 다음
-- check 가 rebuild 를 검증하게 한다. 월 첫 deduct(정상 생성)에도 세워지지만
-- rebuild 는 floor 방식이라 cur >= db+live 이면 no-op — 1회분 DB 조회 비용만
-- 든다(R22 r2 M1 의 additive 는 이중가산으로 폐기됐다).
local usage_existed = redis.call('EXISTS', usage_key)
redis.call('INCRBYFLOAT', usage_key, cost)
redis.call('EXPIRE', usage_key, 3888000)
if usage_existed == 0 then
    redis.call('SET', usage_key .. ':lost', '1', 'EX', 3888000)
end

-- 임계값 교차 체크 — **가장 높은** 교차값을 반환한다.
-- 옛 코드는 첫 교차에서 break 했는데, 한 요청이 75%→105% 처럼 여러 임계값을
-- 건너면 80 만 보고되고 90/100 은 이후 요청에서도 교차로 인식되지 않아
-- "예산 100% 도달" 알림이 영구 누락됐다(알림은 교차 이벤트당 1회 발송).
local triggered = cjson.null
if limit > 0 then
    local old_pct = (used / limit) * 100
    local new_pct = (new_used / limit) * 100
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
    new_used = new_used,
    remaining = limit - new_used,
    threshold_triggered = triggered,
    app_clients = app_clients
})
