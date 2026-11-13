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
-- ARGV[4] = 예약 당시 예상비용 ('' = 미상 — 구버전 호출자). 마커가 TTL 만료로
-- ARGV[5] = done 툼스톤 TTL 초 (미지정 14400)
--           사라진 뒤 호출됐을 때 이 값으로 델타를 복구한다.
--
-- Returns: JSON {settled, recovered, new_used, remaining, threshold_triggered,
--                app_clients}
--
-- 마커 부재의 두 경우를 'budget:settled:' 툼스톤으로 구별한다:
--   · 툼스톤 있음 → 이미 정산됨(settle/환불 이겼음) → no-op 이 정답
--   · 툼스톤 없음 → 마커 TTL 만료/축출 — 예약치는 카운터에 **남아 있다**.
--     settled=false 로 그냥 끝내면 actual > reserved 인 요청(멀티턴 웹서치
--     루프 등)이 그 달 내내 과소 계상된다. 호출자가 예약치를 알면(ARGV[4])
--     정상 settle 과 같은 델타를 적용해 복구한다.

local usage_key  = KEYS[1]
local config_key = KEYS[2]
local marker_key = KEYS[3]
local actual = tonumber(ARGV[1])
-- 음수 실비는 usage 를 내려 한도를 부푼다 — 환불 경로는 actual=0 이므로
-- 음수 입력은 설계 밖이다(deduct 의 cost 가드와 대칭, 12차 리뷰).
if actual and actual < 0 then actual = 0 end
-- 정산 완료 툼스톤은 budget:pending: 네임스페이스 **밖**에 둔다 —
-- "budget:pending:*" 를 스캔하는 경로(reconcile/웜업 pending 합산/복원
-- 스크립트)가 값이 실비인 툼스톤을 라이브 마커로 오인해 정산분을 재합산
-- 했다(R23 r4 M-1). 같은 해시태그를 유지하므로 슬롯은 동일.
local settled_key = 'budget:settled:' .. string.sub(marker_key, 16)

local reserved_raw = redis.call('GET', marker_key)
local recovered = false
if not reserved_raw then
    if redis.call('GET', settled_key) then
        -- 이미 정산된 요청 — 멱등성 보장의 정상 경로
        return cjson.encode({settled = false})
    end
    -- 마커 소실(만료/축출) — 호출자가 예약치를 모르면 안전하게 no-op
    local caller_reserved = tonumber(ARGV[4])
    -- 비유한/음수 호출자 예약치는 복구 근거로 쓸 수 없다 — min(NaN, resvsum)
    -- 은 NaN 을, min(-5, resvsum) 은 -5 를 예약치로 삼아 delta=actual+5 로
    -- 과차감하고 resvsum 에 +5 를 더한다(12차 리뷰 + 13차 후속).
    if caller_reserved and (caller_reserved ~= caller_reserved
        or caller_reserved == math.huge or caller_reserved == -math.huge
        or caller_reserved < 0) then
        caller_reserved = nil
    end
    if not caller_reserved then
        return cjson.encode({settled = false})
    end
    -- ⚠️ "우리 예약이 아직 카운터에 남아 있는가" 를 먼저 확인한다.
    --   예약이 서버에서 실행된 적 없거나 reconcile/rebuild 가 이미 인덱스를
    --   청소했으면 호출자 예약치를 그대로 빼는 것은 받지도 않은 예약의 차감
    --   — 카운터가 과소로 간다(R23 r2 S-2/S-3).
    --   인덱스 필드는 reserve 의 HSET 으로만 생기고 settle/rebuild/reconcile
    --   의 HDEL 로만 지워지므로, 존재 자체가 "미정리 예약" 의 증거다. 생존이
    --   확인된 경우에도 차감은 resvsum 잔량으로 상한을 둔다 — 부분 정리/손상
    --   시 무제한 차감을 막는다.
    --   ⚠️ 알려진 한계(보수 방향): rebuild/reconcile 은 resvsum·인덱스를
    --   정리하지만 usage 카운터는 플로어 설계라 고아 예약치가 **남는다**.
    --   그 뒤의 settle 은 reserved=0 → 실비만 적용하므로 usage 에 고아분이
    --   과대 계상된 채 남을 수 있다(R23 r3 N-2). 과소(차단 우회)보다
    --   과대(조기 차단)가 안전한 방향이다.
    -- NaN/±Inf 정규화 — 'nan' resvsum 은 `or 0` 을 truthy 로 통과하고
    -- min(basis, nan) 이 NaN 을 예약치로 삼아 delta=NaN → INCRBYFLOAT 거부로
    -- 마커는 남은 채 매 settle 이 실패한다(12차 리뷰 F1). 비유한 값은
    -- nil/0(복구 불가 판정 → 실비만 적용)으로 수렴.
    local resvsum = tonumber(redis.call('GET', KEYS[4]) or '0')
    if type(resvsum) ~= 'number' or resvsum ~= resvsum or resvsum == math.huge
        or resvsum == -math.huge or resvsum < 0 then
        resvsum = 0
    end
    local live, idx_reserved
    if KEYS[5] and ARGV[3] and ARGV[3] ~= '' then
        local idx_val = redis.call('HGET', KEYS[5], ARGV[3])
        live = idx_val ~= false
        idx_reserved = tonumber(idx_val or 'x')  -- 숫자 아닌 값은 nil → 폴백
        -- 음수도 폴백 — min(음수, resvsum) 이 음수 예약치를 만들어
        -- delta=actual+5 과차감 + resvsum +5 부풀림(13차 후속).
        if idx_reserved and (idx_reserved ~= idx_reserved
            or idx_reserved == math.huge or idx_reserved == -math.huge
            or idx_reserved < 0) then
            idx_reserved = nil
        end
    else
        -- 인덱스 미제공 호출자(구버전)는 resvsum 잔량을 생존 근거로 삼는다.
        live = resvsum > 0
    end
    if live and resvsum > 0 then
        -- 예약치의 진실 계층: 인덱스 필드값(reserve 가 쓴 실제 예약액 —
        -- 클램프 후, TTL 이 마커의 2배라 더 오래 산다) > caller 의 추정치
        -- (부분 예약이면 클램프 전 값이라 과대 — resvsum 은 스코프 전체
        -- 합계라 min 적용 시 타 요청 예약까지 끌어다 뺀다. 8차 리뷰 M1).
        local basis = idx_reserved or caller_reserved
        reserved_raw = tostring(math.min(basis, resvsum))
    else
        reserved_raw = '0'  -- 이미 걷어졌거나 미실행 — 실비만 적용
    end
    -- 마커 부재 경로 자체가 진단 신호 — 예약치 복구든 실비만 적용이든
    -- 호출자가 알아야 한다.
    recovered = true
end
local reserved = tonumber(reserved_raw) or 0
if reserved ~= reserved or reserved == math.huge or reserved == -math.huge
    or reserved < 0 then
    -- 'nan'/음수 마커·인덱스 — 델타를 실비 전액으로(실비만 적용). 음수는
    -- delta=actual+5 과차감 + resvsum 부풀림이라 같은 0 수렴(13차 후속).
    reserved = 0
end

local config_raw = redis.call('GET', config_key)
if not config_raw and ARGV[2] and ARGV[2] ~= '' then
    config_raw = ARGV[2]
end
local limit = 0
local thresholds = {80, 90, 100}
local app_clients = {}
if config_raw then
    local ok, config = pcall(cjson.decode, config_raw)
    if ok and type(config) == 'table' then
        limit = tonumber(config.limit_usd) or 0
        -- NaN/±Inf 는 아래 비교를 모두 거짓으로 통과해 cjson.encode 직전까지
        -- 도달한다 — 비유한 limit 는 0(차단)으로 수렴(11차 리뷰).
        if limit ~= limit or limit == math.huge or limit == -math.huge then
            limit = 0
        end
        if type(config.thresholds) == 'table' then
            thresholds = config.thresholds
        end
        if config.app_clients then
            app_clients = config.app_clients
        end
    end
end

local used_raw = redis.call('GET', usage_key) or '0'
local used = tonumber(used_raw)
-- '0x10'/' 5' 등 Lua 파서는 받지만 INCRBYFLOAT 가 거부하는 비표준 형식 —
-- 아래 INCRBYFLOAT 가 저장값 파싱에서 죽어 델타가 영구 미적용된다.
-- 표준형으로 재기록해 치유한다(값 보존, 17차 리뷰 F-1).
if used and used_raw ~= tostring(used) then
    redis.call('SET', usage_key, tostring(used), 'KEEPTTL')
end
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
-- 'nan'/'inf'/비수치 resvsum 은 INCRBYFLOAT 가 거부해 usage 커밋 후 여기서
-- 죽는다 — 마커 DEL·툼스톤에 못 닿아 재시도마다 delta 가 이중 적용된다
-- (15차 리뷰). 오염 저장값은 0 으로 덮어써 치유하고 차감은 아래 음수
-- 플로어가 정리한다.
local rs_raw = redis.call('GET', KEYS[4]) or '0'
local rs_stored = tonumber(rs_raw)
-- tonumber('abc') 는 nil — `nil ~= nil` 은 false 라 비수치 문자열은
-- 비유한 가드를 그대로 통과해 INCRBYFLOAT 가 거부되고, usage 커밋 후
-- 여기서 죽으면 재시도마다 delta 가 이중 적용된다(16차 리뷰).
if not rs_stored or rs_stored ~= rs_stored or rs_stored == math.huge
    or rs_stored == -math.huge then
    redis.call('SET', KEYS[4], '0')
elseif rs_raw ~= tostring(rs_stored) then
    -- '0x10'/' 5'/'5.' 등 Lua 파서는 받지만 Redis INCRBYFLOAT 파서가
    -- 거부하는 비표준 형식 — 같은 값의 표준형으로 재기록해 치유한다
    -- (값 보존, 17차 리뷰 F-1).
    redis.call('SET', KEYS[4], tostring(rs_stored), 'KEEPTTL')
end
redis.call('INCRBYFLOAT', KEYS[4], -reserved)
local rs_after = tonumber(redis.call('GET', KEYS[4]) or '0')
if rs_after and rs_after < 0 then
    -- 웜업/구버전 경로의 bounded 오차로 음수가 될 수 있다 — 합계의 불변식은
    -- >=0 이라 음수 잔량은 표시 실지출을 어긋나게 한다(R23 r3 N-3).
    redis.call('SET', KEYS[4], '0')
end
redis.call('EXPIRE', KEYS[4], 3888000)
redis.call('DEL', marker_key)
-- 정산 완료 툼스톤 — 마커 부재 시 "정산됨"과 "만료됨"을 구별한다(상단 주석).
-- TTL 은 마커 TTL(ARGV[5], 미지정 시 4h)과 같게 둔다 — 짧으면 툼스톤 소멸
-- 뒤 늦은 중복 settle 이 복구 분기로 들어와 실비를 재적용한다(R23 r2 N-4).
local tomb_ttl = tonumber(ARGV[5]) or 14400
-- ttl <= 0 은 SET ... EX 에서 델타 적용·마커 삭제 후에 크래시해 경합 settle 이
-- 복구 경로로 재적용(이중정산)된다. 상한도 필요하다 — EX 는 int64 초라
-- >9.2e18(또는 NaN/inf)은 같은 지점에서 크래시한다(12차 리뷰). 설정 오류는
-- 큰 TTL 로 수렴.
if not (tomb_ttl > 0 and tomb_ttl <= 2147483647) then tomb_ttl = 14400 end
redis.call('SET', settled_key, ARGV[1], 'EX', tomb_ttl)
if KEYS[5] and ARGV[3] and ARGV[3] ~= '' then
    redis.call('HDEL', KEYS[5], ARGV[3])  -- 인덱스는 베스트에포트 — 마커가 진실
end

-- 논리적 이전값 = 현재 카운터에서 이 요청의 예약분을 뺀 값.
-- 단 유실 경로(usage_lost)에서는 예약치가 카운터와 함께 사라졌으므로
-- used 에서 빼면 안 된다 — used=0 일 때 logical_old=-reserved 가 돼
-- new_used 가 실비보다 예약치만큼 낮게 나오고 임계 교차 알림이 빠진다
-- (R22 r5 N2). 유실 시 논리적 이전값은 0 이다.
local MAXD = 1.7976931348623157e308
local function sat(v)
    -- 유한 저장값끼리의 ± 도 float64 를 넘을 수 있다(-1e308 - 1e308 = -Inf).
    -- NaN/±Inf 는 cjson.encode 가 거부해 — 커밋된 정산(INCRBYFLOAT·마커
    -- DEL·툼스톤은 이미 반영)의 응답 직렬화가 죽고 호출자가 실패로 오인해
    -- lost 센티넬을 오발한다(17차 리뷰). 부호를 보존하는 포화로 수렴.
    if v ~= v then return 0 end
    if v == math.huge then return MAXD end
    if v == -math.huge then return -MAXD end
    return v
end
local logical_old = sat(usage_lost and 0 or (used - reserved))
local logical_new = sat(logical_old + actual)

local triggered = cjson.null
if limit > 0 then
    -- 교차 비교는 곱셈 형태로 — check.lua 의 prior*100 < limit*t 와 같은
    -- 식이다. (old/limit)*100 처럼 나눗셈을 먼저 하면 정확한 경계값에서
    -- 1-ULP 차이로 check 경로와 판정이 갈린다(11차 리뷰).
    local highest = nil
    for _, t in ipairs(thresholds) do
        -- 소수점 임계값은 교차 후 threshold_triggered int 스키마 거부로
        -- finalize 가 커밋 후 크래시 → 원장 누락(17차 리뷰 F2). 정수 수렴.
        if type(t) == 'number' then
            local ti = math.floor(t)
            if logical_old * 100 < limit * ti
                and logical_new * 100 >= limit * ti then
                if highest == nil or ti > highest then
                    highest = ti
                end
            end
        end
    end
    if highest ~= nil then
        triggered = highest
    end
end

return cjson.encode({
    settled = true,
    recovered = recovered,
    new_used = logical_new,
    -- limit - (-MAXD) 도 +Inf 로 넘친다 — 같은 sat 으로 응답을 지킨다.
    remaining = sat(limit - logical_new),
    threshold_triggered = triggered,
    app_clients = app_clients
})
