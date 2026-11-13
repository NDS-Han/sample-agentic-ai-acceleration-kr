-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Rate Limit Multi-Scope Sliding Window Check
--
-- 3-scope fast-fail 원자 체크: USER → TEAM → GLOBAL 순서로 검사하고,
-- 첫 번째 violation에서 즉시 거부 반환 (이후 스코프는 검사 생략).
-- 전 스코프 통과 시에만 모든 카운터를 증가 (all-or-nothing).
--
-- KEYS[i]                 = 스코프 i 의 ZSET 키 (예: {{USER:uid:model}}:rpm)
-- ARGV[1]                 = now_ms
-- ARGV[2]                 = request_id (ZSET member)
-- ARGV[3]                 = window_ms (기본 60000)
-- ARGV[4]                 = N (스코프 수)
-- ARGV[5..(4+N)]          = 스코프별 limit (nil/0 이면 체크 스킵)
-- ARGV[(5+N)..(4+2N)]     = 스코프별 이름 ('USER'/'TEAM'/'GLOBAL') — 에러 응답용
--
-- Returns JSON:
--   통과:  {allowed=true, scope=null, remaining=-1, retry_after=null, window_reset=null}
--   거부:  {allowed=false, scope='USER'|'TEAM'|'GLOBAL', limit=N, remaining=0,
--          retry_after=초, window_reset=unix초}

local now_ms     = tonumber(ARGV[1])
local request_id = ARGV[2]
local window_ms  = tonumber(ARGV[3]) or 60000
-- window_ms <= 0 이면 window_start > now 가 돼 ZREMRANGEBYSCORE 가 미래
-- 멤버를 남기고 과거를 지우는 역전 정리(한도 우회)가 된다(12차 리뷰 N4).
if not (window_ms > 0 and window_ms <= 2147483647) then window_ms = 60000 end
local num_scopes = tonumber(ARGV[4])

local window_start = now_ms - window_ms

-- Phase 1: 체크 (side-effect 최소화 — ZREMRANGEBYSCORE는 허용, ZADD는 안 함)
for i = 1, num_scopes do
    local key   = KEYS[i]
    local limit = tonumber(ARGV[4 + i])

    if limit and limit > 0 then
        -- 윈도우 밖 항목 제거 (모든 스코프 공통 cleanup)
        redis.call('ZREMRANGEBYSCORE', key, 0, window_start)
        -- 미래 스코어 멤버는 윈도우 이동으로도 제거되지 않아 카운트를 영구
        -- 부풀리고, 최소 스코어로 선택되면 retry_after 가 수십억 초가 된다
        -- (클라이언트 백오프 중독, 17차 리뷰 F6). 스코어는 이 스크립트의
        -- 호출 시각으로만 쓰이므로 now 초과분은 오염이다.
        redis.call('ZREMRANGEBYSCORE', key, '(' .. tostring(now_ms), '+inf')

        local current = redis.call('ZCARD', key)
        -- 같은 request_id 의 재입장(폴백 후보)은 기존 멤버를 재사용한다 —
        -- 자기 슬롯이 카운트에 들어 있으면 한도 비교에서 제외해야, 한도 꽉 찬
        -- 상태에서의 폴백이 "자기 자신 때문에" 거절되지 않는다(6차 리뷰 m2).
        if redis.call('ZSCORE', key, request_id .. ':' .. tostring(i)) then
            current = current - 1
        end
        if current >= limit then
            local scope_name = ARGV[4 + num_scopes + i]

            -- retry_after 계산 (가장 오래된 entry + window)
            local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
            local window_reset_ms = now_ms + window_ms
            if #oldest > 0 then
                -- 오염 스코어('nan'/'inf')는 truthy 라 그대로 더하면
                -- retry_after 가 NaN 이 돼 cjson.encode 가 죽는다
                -- (16차 리뷰). 비유한은 호출 시각 기본값으로 둔다.
                local os_ = tonumber(oldest[2])
                if os_ and os_ == os_ and os_ ~= math.huge
                    and os_ ~= -math.huge then
                    window_reset_ms = os_ + window_ms
                end
            end
            -- 미래 스코어가 남아 있어도 retry_after 는 윈도우 폭을 넘지
            -- 않는다(17차 리뷰 F6 — Retry-After 중독 방지).
            window_reset_ms = math.min(window_reset_ms, now_ms + window_ms)
            local retry_after = math.ceil((window_reset_ms - now_ms) / 1000)
            if retry_after < 1 then retry_after = 1 end

            return cjson.encode({
                allowed      = false,
                scope        = scope_name,
                limit_type   = 'rpm',
                limit        = limit,
                remaining    = 0,
                retry_after  = retry_after,
                window_reset = math.ceil(window_reset_ms / 1000)
            })
        end
    end
end

-- Phase 2: 모든 스코프 통과 — 카운터 증가 (atomic)
-- 이미 있는 멤버(폴백 재입장)는 ZADD 를 건너뛰고 added 에 담지 않는다 —
-- 호출자가 "이번 호출이 새로 넣은 스코프"만 되돌리도록 계약을 좁힌다.
-- 멤버를 공유하는 후보의 undo 가 원래 시도의 슬롯까지 지우던 결함 수정(6차 리뷰 M1).
local added = {}
for i = 1, num_scopes do
    local key   = KEYS[i]
    local limit = tonumber(ARGV[4 + i])

    if limit and limit > 0 then
        local member = request_id .. ':' .. tostring(i)
        if not redis.call('ZSCORE', key, member) then
            redis.call('ZADD', key, now_ms, member)
            table.insert(added, i)
            -- 하드 상한(deepdive Q50): score 트림(Phase1)에 더해 멤버 수를 limit 으로 캡.
            -- 정상 흐름(Phase2 는 ZCARD<limit 일 때만 실행)에선 트리거 안 되나, 시계
            -- 스큐·동시성 레이스·stale 멤버로 인한 비정상 증가를 막아 ZCARD/ZRANGE 비용을
            -- limit 에 독립적으로 bound. 가장 오래된(낮은 rank) 초과분 제거, 최신 limit 유지.
            redis.call('ZREMRANGEBYRANK', key, 0, -(limit + 1))
        end
        -- TTL: window + 60초 버퍼 — 재입장이어도 갱신해 활성 윈도우를 유지.
        redis.call('EXPIRE', key, math.ceil(window_ms / 1000) + 60)
    end
end

return cjson.encode({
    allowed      = true,
    scope        = cjson.null,
    limit_type   = cjson.null,
    limit        = -1,
    remaining    = -1,
    retry_after  = cjson.null,
    window_reset = cjson.null,
    -- 이번 호출이 **새로** ZADD 한 스코프 인덱스 — 호출자의 되돌리기(ZREM)는
    -- 이 스코프들에만 적용해야 한다(재사용 멤버를 지우면 원래 시도의 슬롯이 사라짐).
    added        = added
})
