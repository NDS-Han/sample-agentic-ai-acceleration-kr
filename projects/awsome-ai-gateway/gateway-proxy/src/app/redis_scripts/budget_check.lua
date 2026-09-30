-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Check — 단일 scope(user 또는 team) 사전 체크
-- BR-BDG-01~04 + C-1 (TEAM 미설정 = deny) + Q (USER 미설정 = pass)
--
-- Redis Cluster 제약: 하나의 EVAL 은 같은 hash slot 의 KEYS 만 받는다.
-- 따라서 본 스크립트는 single-scope 로만 호출되어야 한다. 호출자 (Python)
-- 가 USER 용 EVAL 1회 + TEAM 용 EVAL 1회를 순차 실행하고 결과를 합친다.
--
-- KEYS[1] = budget:{scope}:{<scope_id>}:<period>   -- usage counter
-- KEYS[2] = budget:config:{scope}:{<scope_id>}     -- config (same hash tag)
-- ARGV[1] = 'user' | 'team'                         -- scope label (미설정 처리 분기)
-- ARGV[2] = fallback config JSON ('' = 없음). user scope 에서 키가 없을 때
--           팀 기본 cap D 를 합성 config 로 주입하는 데 쓴다 (cap_u = A_u ?? D).
--
-- Returns: JSON {allowed, reason, used_usd, remaining_usd, policy,
--               throttle_active, throttle_rpm_pct, threshold_pct,
--               soft_warning, scope, config_present}
-- config_present=false 면 호출자가 scope 별 미설정 정책을 적용해야 함:
--   - 'user' 미설정 → pass-through (Q 정책)
--   - 'team' 미설정 → deny (C-1 정책)

local usage_key  = KEYS[1]
local config_key = KEYS[2]
local scope_label = ARGV[1]

local function decode_or_nil(s)
    if not s then return nil end
    local ok, v = pcall(cjson.decode, s)
    if not ok then return nil end
    return v
end

local function absent_config()
    return cjson.encode({
        allowed = true,
        reason = cjson.null,
        used_usd = 0,
        remaining_usd = 0,
        limit_usd = 0,
        policy = 'hard_block',
        throttle_active = false,
        throttle_rpm_pct = 50,
        threshold_pct = 0,
        soft_warning = false,
        scope = scope_label,
        config_present = false,
        app_clients = {}
    })
end

local cfg_raw = redis.call('GET', config_key)
if not cfg_raw and ARGV[2] and ARGV[2] ~= '' then
    -- 개인 config 없음 + 팀 기본 cap D 존재 → 호출자가 합성한 D config 로 평가.
    -- D 는 팀 키에 저장되고 이 slot 의 키가 아니므로 ARGV 로 넘긴다.
    cfg_raw = ARGV[2]
end
if not cfg_raw then
    return absent_config()
end

local config = decode_or_nil(cfg_raw)
if not config then
    -- 잘못된 cache 는 "미설정" 과 동일 취급
    return absent_config()
end

-- F3: team scope 에서 limit_usd 가 null 이면 'T=NULL (D-only) 행' 이 캐시된 것 —
-- 설정 없음과 동일하게 config_present=false 를 돌려 호출자가 team_budget_unset
-- 으로 fail-close 하게 한다 (§3-1). 숫자 0 은 명시적 차단이므로 그대로 둔다.
if scope_label == 'team'
    and (config.limit_usd == nil or config.limit_usd == cjson.null) then
    return absent_config()
end

local limit = tonumber(config.limit_usd) or 0
local policy = config.policy or 'hard_block'
local soft_limit_pct = tonumber(config.soft_limit_pct) or 110
local throttle_rpm_pct = tonumber(config.throttle_rpm_pct) or 50
local thresholds = config.thresholds or {80, 90, 100}
local app_clients = config.app_clients or {}

local used = tonumber(redis.call('GET', usage_key) or '0')
local remaining = limit - used
local usage_pct = 0
if limit > 0 then
    usage_pct = math.floor((used / limit) * 100)
end

-- D-8: limit = 0 은 정책과 무관하게 항상 차단 (A_u=0 / D=0 / app_c=0 = 명시적 차단).
-- 예전엔 hard_block/soft_warning 분기만 걸려 throttle 정책에서 0 이 통과됐다.
if limit == 0 then
    return cjson.encode({
        allowed = false,
        reason = scope_label .. '_budget_exceeded',
        used_usd = used,
        remaining_usd = remaining,
        limit_usd = limit,
        policy = policy,
        throttle_active = false,
        throttle_rpm_pct = throttle_rpm_pct,
        threshold_pct = usage_pct,
        soft_warning = false,
        scope = scope_label,
        config_present = true,
        app_clients = app_clients
    })
end

if policy == 'hard_block' and used >= limit then
    return cjson.encode({
        allowed = false,
        reason = scope_label .. '_budget_exceeded',
        used_usd = used,
        remaining_usd = remaining,
        limit_usd = limit,
        policy = policy,
        throttle_active = false,
        throttle_rpm_pct = throttle_rpm_pct,
        threshold_pct = usage_pct,
        soft_warning = false,
        scope = scope_label,
        config_present = true,
        app_clients = app_clients
    })
end

local soft_warning = false
if policy == 'soft_warning' then
    -- ⚠️ 나눗셈을 쓰지 않는다. `limit * (soft_limit_pct / 100)` 은 IEEE754 에서
    --    100 * (110/100) = 110.00000000000001 이 되어, used=110 / limit=100 /
    --    soft_limit_pct=110 이라는 **정확히 경계인** 상태를 통과시킨다. DB 폴백은 Decimal
    --    로 정확히 110 을 계산해 차단하므로, Redis 가 살아 있는지에 따라 판정이 갈렸다
    --    (실측으로 잡은 불일치). 양변에 100 을 곱해 비교하면 표현 오차가 사라진다.
    local effective_limit = limit * soft_limit_pct / 100
    if used * 100 >= limit * soft_limit_pct then
        return cjson.encode({
            allowed = false,
            reason = scope_label .. '_soft_limit_exceeded',
            used_usd = used,
            remaining_usd = remaining,
            limit_usd = limit,
            policy = policy,
            throttle_active = false,
            throttle_rpm_pct = throttle_rpm_pct,
            threshold_pct = usage_pct,
            soft_warning = false,
            scope = scope_label,
            config_present = true,
            app_clients = app_clients
        })
    end
    if used >= limit then soft_warning = true end
end

local throttle_active = false
if policy == 'throttle' then
    for _, t in ipairs(thresholds) do
        if usage_pct >= t then throttle_active = true; break end
    end
end

return cjson.encode({
    allowed = true,
    reason = cjson.null,
    used_usd = used,
    remaining_usd = remaining,
    limit_usd = limit,
    policy = policy,
    throttle_active = throttle_active,
    throttle_rpm_pct = throttle_rpm_pct,
    threshold_pct = usage_pct,
    soft_warning = soft_warning,
    scope = scope_label,
    config_present = true,
    app_clients = app_clients
})
