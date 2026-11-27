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
-- ARGV[1] = 'user' | 'team' | 'client'                         -- scope label (미설정 처리 분기)
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
if type(config) ~= 'table' then
    -- 디코드 실패(nil) 뿐 아니라 스칼라('5','"x"')·null 도 "미설정" 과
    -- 동일 취급 — 비테이블 config 는 아래 config.limit_usd 인덱싱에서
    -- eval 이 죽는다(10차 리뷰).
    return absent_config()
end

-- F3: team scope 에서 limit_usd 가 null 이면 'T=NULL (D-only) 행' 이 캐시된 것 —
-- 설정 없음과 동일하게 config_present=false 를 돌려 호출자가 team_budget_unset
-- 으로 fail-close 하게 한다 (§3-1). 숫자 0 은 명시적 차단이므로 그대로 둔다.
if scope_label == 'team'
    and (config.limit_usd == nil or config.limit_usd == cjson.null) then
    return absent_config()
end

local function normalize_layer(raw)
    local limit = tonumber(raw.limit_usd) or 0
    -- NaN/±Inf 는 아래 비교를 모두 거짓으로 통과해 cjson.encode 직전까지
    -- 도달한다 — 비유한 limit 는 0(차단)으로 수렴(11차 리뷰).
    if limit ~= limit or limit == math.huge or limit == -math.huge then
        limit = 0
    end
    local policy = raw.policy or 'hard_block'
    if policy ~= 'hard_block' and policy ~= 'soft_warning' and policy ~= 'throttle' then
        -- 미지 정책은 어느 차단 분기도 안 타 통과(fail-open)가 되므로 보수적으로
        -- 하드캡 취급(8차 리뷰 — reserve 와 같은 클래스).
        policy = 'hard_block'
    end
    local soft_limit_pct = tonumber(raw.soft_limit_pct) or 110
    local throttle_rpm_pct = tonumber(raw.throttle_rpm_pct) or 50
    -- tonumber('nan') 는 truthy NaN 이라 or 폴백이 못 잡는다 — NaN pct 는
    -- 모든 비교를 거짓으로 만들어 soft cap/스로틀이 조용히 무력화된다
    -- (limit_usd 의 11차 가드와 같은 클래스, 13차 리뷰).
    if soft_limit_pct ~= soft_limit_pct or soft_limit_pct == math.huge
        or soft_limit_pct == -math.huge then soft_limit_pct = 110 end
    if throttle_rpm_pct ~= throttle_rpm_pct or throttle_rpm_pct == math.huge
        or throttle_rpm_pct == -math.huge then throttle_rpm_pct = 50 end
    -- cjson.null 은 truthy userdata 라 or 로 못 걸러진다 — 비테이블
    -- (오염 캐시의 null/숫자 등)이 오면 아래 ipairs 가 eval 실패한다(9차 리뷰).
    local thresholds = {80, 90, 100}
    if type(raw.thresholds) == 'table' then
        thresholds = raw.thresholds
    end
    return {
        limit = limit,
        policy = policy,
        soft_limit_pct = soft_limit_pct,
        throttle_rpm_pct = throttle_rpm_pct,
        thresholds = thresholds,
    }
end

-- mixed per-app/app-group 정책: 같은 client 카운터를 두 정책이 공유한다.
-- layers 가 있으면 각 행의 차단 조건을 모두 평가한다 — 낮은 명목 금액의
-- THROTTLE/SOFT_WARNING 행이 다른 행의 HARD_BLOCK 을 지우지 못한다.
local layers = {}
if type(config.layers) == 'table' then
    for _, raw_layer in ipairs(config.layers) do
        if type(raw_layer) == 'table' then
            table.insert(layers, normalize_layer(raw_layer))
        end
    end
end
if #layers == 0 then
    table.insert(layers, normalize_layer(config))
end
local app_clients = config.app_clients or {}

local used = tonumber(redis.call('GET', usage_key) or '0')

local function evaluate_layer(layer)
    local remaining = layer.limit - used
    local usage_pct = 0
    if layer.limit > 0 then
        -- Python DB 폴백(_evaluate_layer)과 같은 divide-first 순서 —
        -- int(used/limit*100). 곱셈-먼저는 정수 경계에서 1-ULP 차이로
        -- Redis/DB 판정이 갈린다(11차 리뷰 — 곱셈-먼저 변경은 회귀였다).
        usage_pct = math.floor((used / layer.limit) * 100)
    end

    -- D-8: limit = 0 은 정책과 무관하게 항상 차단 (A_u=0 / D=0 / app_c=0 = 명시적 차단).
    -- 예전엔 hard_block/soft_warning 분기만 걸려 throttle 정책에서 0 이 통과됐다.
    if layer.limit <= 0 then
        return {
            allowed = false, reason = scope_label .. '_budget_exceeded',
            layer = layer, remaining = remaining, usage_pct = usage_pct,
            block_cap = 0,
        }
    end

    -- reserve 와 같은 ε 경계 — 잔차(5e-10 같은 부동소수 찌꺼기)를 '가용'으로
    -- 보여주지 않게 둘을 맞춘다(9차 리뷰). 종국 판정은 어차피 reserve 가 내린다.
    if layer.policy == 'hard_block' and used >= layer.limit - 1e-9 then
        return {
            allowed = false, reason = scope_label .. '_budget_exceeded',
            layer = layer, remaining = remaining, usage_pct = usage_pct,
            block_cap = layer.limit,
        }
    end

    local soft_warning = false
    if layer.policy == 'soft_warning' then
        -- ⚠️ 나눗셈을 쓰지 않는다. `limit * (soft_limit_pct / 100)` 은 IEEE754 에서
        --    100 * (110/100) = 110.00000000000001 이 되어, used=110 / limit=100 /
        --    soft_limit_pct=110 이라는 **정확히 경계인** 상태를 통과시킨다. DB 폴백은 Decimal
        --    로 정확히 110 을 계산해 차단하므로, Redis 가 살아 있는지에 따라 판정이 갈렸다
        --    (실측으로 잡은 불일치). 양변에 100 을 곱해 비교하면 표현 오차가 사라진다.
        if used * 100 >= layer.limit * layer.soft_limit_pct then
            return {
                allowed = false, reason = scope_label .. '_soft_limit_exceeded',
                layer = layer, remaining = remaining, usage_pct = usage_pct,
                block_cap = (layer.limit * layer.soft_limit_pct) / 100,
            }
        end
        if used >= layer.limit then soft_warning = true end
    end

    local throttle_active = false
    if layer.policy == 'throttle' then
        for _, t in ipairs(layer.thresholds) do
            if type(t) == 'number' and usage_pct >= t then
                throttle_active = true
                break
            end
        end
    end

    local effective_limit = layer.limit
    if layer.policy == 'soft_warning' then
        effective_limit = (layer.limit * layer.soft_limit_pct) / 100
    end
    return {
        allowed = true,
        layer = layer,
        remaining = effective_limit - used,
        display_remaining = remaining,
        usage_pct = usage_pct,
        soft_warning = soft_warning,
        throttle_active = throttle_active,
    }
end

local function encode_result(result, soft_warning, throttle_active, throttle_rpm_pct)
    local layer = result.layer
    return cjson.encode({
        allowed = result.allowed,
        reason = result.reason or cjson.null,
        used_usd = used,
        remaining_usd = result.display_remaining or result.remaining,
        limit_usd = layer.limit,
        policy = layer.policy,
        throttle_active = throttle_active,
        throttle_rpm_pct = throttle_rpm_pct or layer.throttle_rpm_pct,
        threshold_pct = result.usage_pct,
        soft_warning = soft_warning,
        scope = scope_label,
        config_present = true,
        app_clients = app_clients
    })
end

local blocked = nil
local allowed_results = {}
for _, layer in ipairs(layers) do
    local result = evaluate_layer(layer)
    if result.allowed then
        table.insert(allowed_results, result)
    elseif blocked == nil
        or result.block_cap < blocked.block_cap
        or (result.block_cap == blocked.block_cap and result.layer.policy == 'hard_block') then
        blocked = result
    end
end

if blocked ~= nil then
    return encode_result(blocked, false, false, blocked.layer.throttle_rpm_pct)
end

-- 차단이 없으면 warning/throttle 신호는 각 정책의 합집합이다. 표시 계층은
-- _build_status 와 같은 규칙으로 "차단까지의 잔여"가 가장 작은 계층을 고른다.
local soft_warning_any = false
local throttle_active_any = false
local throttle_rpm_min = nil
local decisive = nil
for _, result in ipairs(allowed_results) do
    if result.soft_warning then soft_warning_any = true end
    if result.throttle_active then
        throttle_active_any = true
        local rpm = result.layer.throttle_rpm_pct
        if throttle_rpm_min == nil or rpm < throttle_rpm_min then
            throttle_rpm_min = rpm
        end
    end
    if result.layer.policy ~= 'throttle' then
        if decisive == nil or result.remaining < decisive.remaining then
            decisive = result
        end
    end
end
if decisive == nil then
    -- 전 계층이 THROTTLE — 금액 차단이 없으므로 명목 잔여가 가장 작은
    -- 계층을 표시용으로 고른다(기존 단일 throttle 행과 같은 의미).
    for _, result in ipairs(allowed_results) do
        if decisive == nil or result.display_remaining < decisive.display_remaining then
            decisive = result
        end
    end
end

return encode_result(
    decisive,
    soft_warning_any,
    throttle_active_any,
    throttle_rpm_min or decisive.layer.throttle_rpm_pct
)
