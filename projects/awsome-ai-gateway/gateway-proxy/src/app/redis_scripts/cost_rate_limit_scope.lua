-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Cost Rate Limit — 단일 스코프 CPM/CPH 사전 예약 (USER 또는 TEAM)
-- FR-4.6 / BR-RL-03
--
-- Redis Cluster CROSSSLOT 대응(deepdive Q50): USER/TEAM 키는 hash tag 가 달라
-- 슬롯이 다르므로 한 eval 에 묶을 수 없다. 이 스크립트는 **한 스코프의 2키만**
-- 받는다(cpm/cph, 동일 {scope_id} hash tag = 단일 슬롯). 호출자가 scope 별로
-- 1회씩 호출하고 결과를 합친다(rate_limit_service.reserve_cost). 과거의
-- 4키(user+team) 단일 eval(cost_rate_limit.lua)은 cluster 에서 CROSSSLOT →
-- fail-open 으로 CPM/CPH 무음 미집행됐다.
--
-- KEYS[1] = rl:cost:<scope>:{<scope_id>}:cpm:<window_ts>
-- KEYS[2] = rl:cost:<scope>:{<scope_id>}:cph:<window_ts>
--
-- ARGV[1] = estimated_cost (USD, string decimal)
-- ARGV[2] = cpm_limit  (0 또는 음수 = unlimited)
-- ARGV[3] = cph_limit
-- ARGV[4] = scope_label ('USER' | 'TEAM')
--
-- Returns: JSON {allowed, scope, limit_type, limit, remaining, retry_after, reserved_cost}
--   통과 시 카운터(INCRBYFLOAT)까지 커밋하고 reserved_cost 반환.

local cost        = tonumber(ARGV[1])
-- 음수 추정치는 아래 cost>0 게이트가 커밋을 건너뛰는데 reserved_cost=음수 가
-- 반환돼 settle 의 (actual-reserved) 델타가 실비보다 크게 적용된다 — 0 으로
-- 수렴시키면 예약도 반환값도 일관된다(12차 리뷰).
-- NaN/Inf 도 같은 경로다 — NaN 은 cost>0 게이트를 빠져 커밋 없이
-- reserved_cost=NaN 이 encode 를 죽이고, Inf 는 INCRBYFLOAT 가 거부해
-- eval 이 죽는다(17차 리뷰).
if cost and (cost < 0 or cost ~= cost
    or cost == math.huge or cost == -math.huge) then cost = 0 end
local cpm_limit   = tonumber(ARGV[2])
local cph_limit   = tonumber(ARGV[3])
local scope_label = ARGV[4]

-- 오염 버킷('nan'/'inf'/'abc'/'0x10') 치유 — 그대로 두면 INCRBYFLOAT 가
-- 거부해 (a) KEYS[2] 오염 시 KEYS[1] 커밋 후 죽는 부분커밋 팬텀,
-- (b) KEYS[1] 오염 시 eval 실패로 윈도우 내내 fail-open 이 된다(17차 F5).
-- 비유한/비수치는 0 으로, 비표준 형식은 표준형으로 재기록해 값을 보존한다.
local function _bkt(key)
    local raw = redis.call('GET', key) or '0'
    local v = tonumber(raw)
    if not v or v ~= v or v == math.huge or v == -math.huge then
        redis.call('SET', key, '0', 'KEEPTTL')
        return 0
    end
    if raw ~= tostring(v) then
        redis.call('SET', key, tostring(v), 'KEEPTTL')
    end
    return v
end
local cpm_used = _bkt(KEYS[1])
local cph_used = _bkt(KEYS[2])

local cpm_after = cpm_used + cost
local cph_after = cph_used + cost

if cpm_limit and cpm_limit > 0 and cpm_after > cpm_limit then
    return cjson.encode({
        allowed = false,
        scope = scope_label,
        limit_type = "cpm",
        limit = cpm_limit,
        -- limit - (-1e308) 같은 뺄셈이 +Inf 로 넘쳐 encode 를 죽이지
        -- 않게 상한을 limit 으로 둔다 — 잔여는 어차피 limit 이 최대(17차).
        remaining = math.max(0, math.min(cpm_limit, cpm_limit - cpm_used)),
        retry_after = 60,
        reserved_cost = 0,
    })
end
if cph_limit and cph_limit > 0 and cph_after > cph_limit then
    return cjson.encode({
        allowed = false,
        scope = scope_label,
        limit_type = "cph",
        limit = cph_limit,
        remaining = math.max(0, math.min(cph_limit, cph_limit - cph_used)),
        retry_after = 3600,
        reserved_cost = 0,
    })
end

-- 통과 — 예약 커밋 (INCRBYFLOAT + EXPIRE)
if cost > 0 then
    redis.call('INCRBYFLOAT', KEYS[1], cost)
    redis.call('EXPIRE', KEYS[1], 120)
    redis.call('INCRBYFLOAT', KEYS[2], cost)
    redis.call('EXPIRE', KEYS[2], 7200)
end

return cjson.encode({
    allowed = true,
    reserved_cost = cost,
})
