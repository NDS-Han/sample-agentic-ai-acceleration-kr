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
--   reserved=true 경로에만: reserved_usd(실제 예약액 — 잔여 클램프 반영),
--     partial(클램프 발생 여부), dedup_hit(멱등 재생) 이 실린다.
--   settled_before 는 reserved=false 툼스톤 통과 경로에서만 실린다.
--   config_present=false → 호출자가 scope 별 미설정 정책 적용
--     (user/client: pass-through, team: deny — budget_check.lua 와 동일)

local usage_key  = KEYS[1]
local config_key = KEYS[2]
local marker_key = KEYS[3]
local scope_label = ARGV[1]
local estimate = tonumber(ARGV[2]) or 0
if estimate < 0 then estimate = 0 end  -- 음수 추정치가 usage 를 내리는 것 방어
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

-- request_id 멱등 재생 가드: 이 요청의 마커가 이미 있으면 예약은 커밋된
-- 상태다 — 클라이언트 레벨 재시도(응답 단절 후 eval 재전송, retry_on_error)
-- 가 같은 request_id 를 다시 보내도 카운터를 이중으로 올리지 않는다.
-- 응답은 커밋 시점과 같은 allowed/reserved=true 로 재생해 호출자의
-- 롤백 계약(committed 디스크립터 등록)을 유지한다. limit 은 재계산하지
-- 않는다 — 호출자의 분기는 allowed/reserved/config_present 만 본다.
-- 마커 부재시에만 아래로 진행해 실제 쓰기를 하므로, 중간 크래시로 마커가
-- 없이 끝난 재실행은 예외 경로(환불/lost 센티넬)로 흘러 조용한 과소계상
-- 방향이 아니라 복구 가능한 방향으로 어긋난다.
if redis.call('EXISTS', marker_key) == 1 then
    -- ⚠️ reserved_usd 를 빼면 안 된다: 디스크립터는 이 값(실제 예약액)을
    -- settle 복구 ARGV[4] 로 들고 있는데, 부분 예약 요청의 재전송이 dedup 으로
    -- 오면 폴백 추정치(>클램프값)가 저장돼, 마커 소실 뒤 settle 복구가
    -- resvsum 에서 타 요청 예약까지 과차감한다(8차 리뷰 M1, 실Redis 재현).
    return cjson.encode({
        allowed = true,
        reserved = true,
        reason = cjson.null,
        used_usd = tonumber(redis.call('GET', usage_key) or '0'),
        limit_usd = 0,
        scope = scope_label,
        config_present = true,
        dedup_hit = true,
        reserved_usd = tonumber(redis.call('GET', marker_key)),
        partial = cjson.null,
    })
end

-- 늦은 도착 가드: 클라이언트가 타임아웃으로 이 eval 을 포기하면 호출부는
-- 환불 settle(0) 을 보낸다. 그 settle 이 먼저 실행돼 settled 툼스톤을
-- 쓴 뒤 이 reserve 가 도착하면, 커밋되는 예약은 아무도 정산하지 않는
-- 고아가 돼 marker_ttl 동안 resvsum 을 부풀린다. 툼스톤이 있으면
-- "예약 없이 통과"로 응답한다 — 이 응답은 대부분 버려지지만, 재시도가
-- 툼스톤을 만난 경우에도 호출부는 reserved=false 로 디스크립터를
-- 등록하지 않아 일관된다.
if redis.call('GET', 'budget:settled:' .. string.sub(marker_key, 16)) then
    return cjson.encode({
        allowed = true,
        reserved = false,
        reason = cjson.null,
        used_usd = tonumber(redis.call('GET', usage_key) or '0'),
        limit_usd = 0,
        scope = scope_label,
        config_present = true,
        settled_before = true,
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
if policy ~= 'hard_block' and policy ~= 'soft_warning' and policy ~= 'throttle' then
    -- 미지 정책(admin enum 이 Lua 지원보다 먼저 나가는 롤아웃 창, 오타)은
    -- cap=nil 무차단이 되므로 보수적으로 하드캡 취급(8차 리뷰).
    policy = 'hard_block'
end
local soft_limit_pct = tonumber(config.soft_limit_pct) or 110

local used = tonumber(redis.call('GET', usage_key) or '0')

-- D-8: limit = 0 은 정책과 무관하게 항상 차단.
if limit == 0 then
    return reply(false, false, scope_label .. '_budget_exceeded', used, limit, true)
end

-- 정책별 상한 cap 을 먼저 정한다 — hard_block=limit, soft_warning=limit×pct,
-- throttle=무차단(cap 없음). 소진 판정은 정책마다 형태가 다르다:
--   hard_block: INCRBYFLOAT 표현오차(9.9+0.1=9.999…64)로 used 가 cap 바로
--     아래 착지하면 다음 요청이 ε 예약으로 입장한다 — ε 이하 잔차는 소진으로
--     본다(8차 리뷰 m2).
--   soft_warning: check 와 같은 곱셈 경계식(×100) — 나눗셈 cap 비교는
--     1-ULP 발산으로 check 가 막는 경계에서 reserve 가 통과시킨다(8차 리뷰
--     m1, 브루트포스 35/2808 발산).
local cap = nil
local at_cap = false
if policy == 'hard_block' then
    cap = limit
    at_cap = used >= cap - 1e-9
elseif policy == 'soft_warning' then
    cap = (limit * soft_limit_pct) / 100
    at_cap = used * 100 >= limit * soft_limit_pct
end

local reserved_amt = estimate
if cap then
    -- 잔여 소진 = 실제로 상한을 다 쓴 상태 — 이 경우만 차단한다.
    -- worst-case 추정이 잔여보다 커서 막히던 구조(비싼 모델+작은 예산에서
    -- 실비 $0.5 호출이 worst $7 평가로 거절되던 것)를 잔여 클램프로 완화:
    -- 예약은 잔여만큼만 잡고 입장시키며, 초과분은 settle 실비가 캡 위로
    -- 기록해 다음 요청이 차단된다 — "마지막 요청 완주" 의미론이다.
    -- 초과량 상한은 worst-case 가 아니라 그 요청의 실비다.
    if at_cap then
        return reply(false, false, scope_label .. '_budget_exceeded',
                     used, limit, true)
    end
    if estimate > cap - used then
        reserved_amt = math.max(cap - used, 0)
    end
end

-- 카운터 유실 감지(R22 r1 M1 / r2 S7): 이 reserve 가 usage 키를 **생성**하면
-- (체크 통과 시점엔 있었거나, 이번 달 첫 쓰기거나, 축출/failover 로 사라졌거나)
-- 센티넬을 세운다. resvsum 동시 유실로 pre>0 조건이 못 잡던 케이스까지
-- 커버된다. 월 첫 예약의 정상 생성에도 세워지지만 rebuild 가 플로어
-- 방식(3세대 budget_rebuild.lua)이라 결과는 no-op+센티넬 해제 — 스코프·월당
-- 1회분 DB 조회만 든다.
local usage_existed = redis.call('EXISTS', usage_key)

-- 예약 커밋: 카운터에 **실제 예약액**(잔여 클램프 반영분)을 올리고 마커를
-- 남긴다. 마커가 settle 의 멱등성 근거 — settle 은 마커가 있을 때만 델타를
-- 적용하므로 이중 정산(finalize 와 release_reservations 의 경쟁 호출, 혹은
-- 재시도)이 안전하다. 마커 값은 반드시 reserved_amt — settle 이 읽어
-- (실비 - 예약액) 델타를 빼는데, 추정치를 쓰면 클램프분이 정산에서 샌다.
redis.call('INCRBYFLOAT', usage_key, reserved_amt)
redis.call('EXPIRE', usage_key, 3888000)  -- 45d (budget_deduct.lua 와 동일)
redis.call('INCRBYFLOAT', KEYS[4], reserved_amt)
redis.call('EXPIRE', KEYS[4], 3888000)
redis.call('SET', marker_key, tostring(reserved_amt), 'EX', marker_ttl)
if KEYS[5] and ARGV[5] and ARGV[5] ~= '' then
    -- 인덱스 TTL 은 마커의 2배 — 마커 TTL 정상만료 직후의 늦은 settle 이
    -- HEXISTS 생존검증을 통과해 예약치를 정확히 정산하게 한다. 필드가
    -- 마커보다 먼저 죽으면 settle 은 복구 분기로 새어 resvsum 이 누수되고
    -- 실비가 과대 계상된다(R23 r3 N-1). 고아 필드는 rebuild/reconcile 이
    -- dead 스윕으로 청소한다.
    redis.call('HSET', KEYS[5], ARGV[5], tostring(reserved_amt))
    redis.call('EXPIRE', KEYS[5], marker_ttl * 2)
end

if usage_existed == 0 then
    redis.call('SET', usage_key .. ':lost', '1', 'EX', 3888000)
end

-- 부분 예약(잔여 클램프)은 호출자가 디스크립터의 "reserved" 로 써야 한다 —
-- settle 복구 경로(ARGV[4])가 원래 추정치를 받으면 클램프분만큼 차감이 샌다.
return cjson.encode({
    allowed = true,
    reserved = true,
    reason = cjson.null,
    used_usd = used + reserved_amt,
    limit_usd = limit,
    scope = scope_label,
    config_present = true,
    reserved_usd = reserved_amt,
    partial = reserved_amt ~= estimate,
})
