-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Reserve — 단일 scope 월예산 원자적 체크 + 최악비용 예약
--
-- 배경: budget_check.lua 는 읽기 전용이라, 동시 N 개 요청이 같은 used 값을
-- 읽고 전부 통과한 뒤 각자 실비를 차감해 한도를 넘는다(check-then-act).
-- 이 스크립트는 체크와 예약을 한 EVAL 로 묶어 그 창을 닫는다 — 사용 카운터에
-- 최악비용 추정치를 먼저 올리고, budget_settle.lua 가 완료 후 (실비-예약) 델타로
-- 정산한다. 예약분이 used 에 포함되므로 기존 budget_check 는 수정 없이
-- in-flight 예약을 자동으로 반영한다.
--
-- KEYS[1] = budget:{scope}:{<scope_id>}:<period>         -- usage counter
-- KEYS[2] = budget:config:{scope}:{<scope_id>}           -- config (same hash tag)
-- KEYS[3] = budget:pending:{scope}:{<scope_id>}:<req_id> -- per-request 예약 마커
-- KEYS[4] = budget:resvsum:{scope}:{<scope_id>}:{period} -- 미정산 예약 합계
--           (usage 카운터는 예약 포함 "확약액" — 표시용 실지출 = usage - resvsum)
-- KEYS[5] = budget:pendingidx:{scope}:{<scope_id>}       -- 마커 인덱스 해시
--           (같은 hash tag — rebuild 의 pending 합산이 키스페이스 SCAN 없이
--           HKEYS+필드별 GET 으로 끝나게 한다. settle 이 HDEL 로 지운다. R22 r1 S1)
-- ARGV[1] = 'user' | 'team' | 'client'                    -- scope label
-- ARGV[2] = estimate (USD, string decimal)               -- 최악비용 추정치
-- ARGV[3] = fallback config JSON ('' = 없음)              -- user D-cap 합성용
-- ARGV[4] = marker TTL seconds                           -- 예약 마커 수명
-- ARGV[5] = index field (req_id; client scope 는 <client>:<req_id>)
--
-- Returns: JSON {allowed, reserved, reason, used_usd, limit_usd,
--               config_present, scope}
--   config_present=false → 호출자가 scope 별 미설정 정책 적용
--     (user/client: pass-through, team: deny — budget_check.lua 와 동일)

local usage_key  = KEYS[1]
local config_key = KEYS[2]
local marker_key = KEYS[3]
local scope_label = ARGV[1]
local estimate = tonumber(ARGV[2]) or 0
local marker_ttl = tonumber(ARGV[4]) or 14400

local function decode_or_nil(s)
    if not s then return nil end
    local ok, v = pcall(cjson.decode, s)
    if not ok then return nil end
    return v
end

local function reply(allowed, reserved, reason, used, limit, config_present)
    return cjson.encode({
        allowed = allowed,
        reserved = reserved,
        reason = reason or cjson.null,
        used_usd = used,
        limit_usd = limit,
        scope = scope_label,
        config_present = config_present,
    })
end

local cfg_raw = redis.call('GET', config_key)
if not cfg_raw and ARGV[3] and ARGV[3] ~= '' then
    cfg_raw = ARGV[3]
end
if not cfg_raw then
    -- 설정 없음 → 예약도 없다. 호출자가 scope 정책(user/client: pass, team: deny) 적용.
    return reply(true, false, nil, 0, 0, false)
end

local config = decode_or_nil(cfg_raw)
if not config then
    return reply(true, false, nil, 0, 0, false)
end

-- F3: team scope 의 limit_usd=null 은 'T=NULL (D-only) 행' — 미설정과 동일 취급.
if scope_label == 'team'
    and (config.limit_usd == nil or config.limit_usd == cjson.null) then
    return reply(true, false, nil, 0, 0, false)
end

local limit = tonumber(config.limit_usd) or 0
local policy = config.policy or 'hard_block'
local soft_limit_pct = tonumber(config.soft_limit_pct) or 110

local used = tonumber(redis.call('GET', usage_key) or '0')

-- D-8: limit = 0 은 정책과 무관하게 항상 차단.
if limit == 0 then
    return reply(false, false, scope_label .. '_budget_exceeded', used, limit, true)
end

local function exceeds()
    if policy == 'hard_block' then
        -- 엄격 하드캡: 예약을 포함한 worst-case 가 한도를 넘으면 거절.
        return used + estimate > limit
    elseif policy == 'soft_warning' then
        -- 체크와 같은 경계식(×100, IEEE754 표현오차 회피 — budget_check.lua 주석 참조).
        -- 예약 포함값이 소프트 상한에 닿으면 거절.
        return (used + estimate) * 100 >= limit * soft_limit_pct
    end
    -- throttle: 차단 없음 — 예약은 그대로 둔다(정산 정확도 목적).
    return false
end

if exceeds() then
    -- 잔량(used<limit)이 있는데 최악비용 예약으로 막힌 경우와 실제 소진을
    -- 구분한다 — 잔량 아래 차단을 "예산 초과"로 보고하면 클라이언트 오보.
    -- 코드는 동일하게 429 지만 reason suffix 로 구분한다(R22 r1 S6).
    local reason = scope_label .. '_budget_exceeded'
    if used < limit then
        reason = scope_label .. '_budget_reservation_exceeded'
    end
    return reply(false, false, reason, used, limit, true)
end

-- 카운터 유실 감지(R22 r1 M1 / r2 S7): 이 reserve 가 usage 키를 **생성**하면
-- (체크 통과 시점엔 있었거나, 이번 달 첫 쓰기거나, 축출/failover 로 사라졌거나)
-- 센티넬을 세운다. resvsum 동시 유실로 pre>0 조건이 못 잡던 케이스까지
-- 커버된다. 월 첫 예약의 정상 생성에도 세워지지만 rebuild 가 플로어
-- 방식(3세대 budget_rebuild.lua)이라 결과는 no-op+센티넬 해제 — 스코프·월당
-- 1회분 DB 조회만 든다.
local usage_existed = redis.call('EXISTS', usage_key)

-- 예약 커밋: 카운터에 추정치를 올리고 마커를 남긴다. 마커가 settle 의
-- 멱등성 근거 — settle 은 마커가 있을 때만 델타를 적용하므로 이중 정산
-- (finalize 와 release_reservations 의 경쟁 호출, 혹은 재시도)이 안전하다.
redis.call('INCRBYFLOAT', usage_key, estimate)
redis.call('EXPIRE', usage_key, 3888000)  -- 45d (budget_deduct.lua 와 동일)
redis.call('INCRBYFLOAT', KEYS[4], estimate)
redis.call('EXPIRE', KEYS[4], 3888000)
redis.call('SET', marker_key, ARGV[2], 'EX', marker_ttl)
if KEYS[5] and ARGV[5] and ARGV[5] ~= '' then
    -- 마커와 같은 수명으로 인덱스 TTL 갱신 — 인덱스가 먼저 죽으면 읽기가
    -- SCAN 폴백으로 돌아간다(정합은 유지, 비용만 커진다).
    redis.call('HSET', KEYS[5], ARGV[5], ARGV[2])
    redis.call('EXPIRE', KEYS[5], marker_ttl)
end

if usage_existed == 0 then
    redis.call('SET', usage_key .. ':lost', '1', 'EX', 3888000)
end

return reply(true, true, nil, used + estimate, limit, true)
