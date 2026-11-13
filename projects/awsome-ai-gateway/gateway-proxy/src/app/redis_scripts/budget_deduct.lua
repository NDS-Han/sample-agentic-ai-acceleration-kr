-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Budget Deduct — 실제 비용 원자적 차감 + 임계값 교차 체크
-- BR-BDG-05, BR-BDG-06
--
-- KEYS[1] = budget:user:{<user_id>}:<period>   -- hash tag on user_id
-- KEYS[2] = budget:config:user:{<user_id>}     -- same hash tag → same slot
-- KEYS[3] = <usage_key>:ded:<request_id>       -- 재실행 감지 마커 (선택)
-- ARGV[1] = cost (USD, string decimal)
-- ARGV[2] = fallback config JSON ('' = 없음). user 키가 없을 때 팀 기본 cap D
--           로 threshold 교차를 평가한다 (D-10: D 적용 유저도 임계값 알림).
-- ARGV[3] = dedup 마커 TTL 초 (선택, 기본 600). 재시도 창은 클라이언트 1회 +
--           앱 레벨 재시도로 수 초 — 600s 는 넉넉한 마진. 요청당 최대 3개 키
--           (user/team/client)라 긴 TTL 은 요청률 곱으로 메모리를 먹는다.
--
-- Returns: JSON {new_used, remaining, threshold_triggered}

local usage_key = KEYS[1]
local config_key = KEYS[2]
local dedup_key = KEYS[3]  -- <usage_key>:ded:{request_id} — 재실행 감지용 (선택)
local cost = tonumber(ARGV[1])
if cost and cost < 0 then cost = 0 end  -- 음수 차감은 usage 를 부풀려 한도를 늘린다(reserve 의 estimate 가드와 대칭, 12차)
local dedup_ttl = tonumber(ARGV[3]) or 600
-- ttl 가드 — 하단의 SET ... EX dedup_key 가 차감 커밋 후에 실행되므로,
-- ttl <= 0/NaN/int64 초과면 마커 미작성으로 죽어 재시도가 이중 차감한다
-- (12차 리뷰 — 현재 호출자는 ARGV[3] 를 보내지 않지만 계약상 방어).
if not (dedup_ttl > 0 and dedup_ttl <= 2147483647) then dedup_ttl = 600 end

local config_raw = redis.call('GET', config_key)
if not config_raw and ARGV[2] and ARGV[2] ~= '' then
    config_raw = ARGV[2]
end
local limit = 0
local thresholds = {80, 90, 100}
local app_clients = {}
if config_raw then
    -- pcall + 테이블 가드 — malformed JSON·스칼라('5' 등)는 차감 전 크래시라
    -- 방향은 안전이었으나 조용한 로그만 남긴다(10차 리뷰).
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

-- request_id 멱등 재생 가드: 같은 요청의 차감이 이미 적용돼 있으면
-- (클라이언트 레벨 재시도가 응답 단절 뒤 eval 을 재전송한 경우) 이중 차감
-- 없이 저장된 첫 실행의 결과를 재생한다. threshold_triggered 도 마커에 담긴
-- 값을 돌려준다 — 알림은 Python 이 eval 응답을 보고 발행하므로, 첫 응답이
-- 유실돼 재실행된 거라면 교차 이벤트는 아직 발행되지 않았다. 재생은 중복이
-- 아니라 영구 누락 방지다(재전송은 응답 유실에서만 발생).
-- 구버전 마커('1')에는 교차 결과가 없어 null 을 유지한다(기존 동작).
if dedup_key and dedup_key ~= '' then
    local replay = redis.call('GET', dedup_key)
    if replay then
        local used_now = tonumber(redis.call('GET', usage_key) or '0')
        local tv = tonumber(string.match(replay, '^t:([%d.]+)$') or '') or 0
        return cjson.encode({
            new_used = used_now,
            remaining = limit - used_now,
            threshold_triggered = (tv > 0) and tv or cjson.null,
            app_clients = app_clients,
            dedup_hit = true,
        })
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
local highest = nil
if limit > 0 then
    -- 교차 비교는 곱셈 형태로 — check.lua 의 prior*100 < limit*t 와 같은
    -- 식이다. 나눗셈-먼저 형태는 정확한 경계값에서 1-ULP 차이로 판정이
    -- 갈린다(11차 리뷰).
    for _, t in ipairs(thresholds) do
        if type(t) == 'number'
            and used * 100 < limit * t
            and new_used * 100 >= limit * t then
            if highest == nil or t > highest then
                highest = t
            end
        end
    end
    if highest ~= nil then
        triggered = highest
    end
end

-- dedup 마커는 차감 + 교차 계산이 끝난 뒤에 세운다. Redis 7 에서 스크립트
-- 효과는 MULTI/EXEC 처럼 원자 복제돼 서버 크래시가 반쪽 적용을 남기지는
-- 않지만, 스크립트 중간의 런타임 오류(OOM 등)는 그 전까지의 쓰기를 남긴다 —
-- 마커를 마지막에 세우면 그 경우 '차감은 됐는데 마커 없음' 상태가 돼 예외
-- 경로(lost_amt/suspect 복구)로 올바르게 흐른다. 값은 't:<highest>' —
-- 재생 시 교차 알림을 재발행하기 위함(없으면 't:0').
if dedup_key and dedup_key ~= '' then
    redis.call('SET', dedup_key, 't:' .. tostring(highest or 0), 'EX', dedup_ttl)
end

return cjson.encode({
    new_used = new_used,
    remaining = limit - new_used,
    threshold_triggered = triggered,
    app_clients = app_clients
})
