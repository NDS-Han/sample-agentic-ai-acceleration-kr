-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
--
-- 예산 카운터 교정 — REDIS_DEGRADED 복구 리컨사일 창에 scope 당 1회 호출.
--
-- 키 의미론:
--   usage   = DB 확정 실비 + 미flush 파이프라인 + 라이브 예약치 + 만료 고아 예약치
--   resvsum = 라이브 예약치 + 만료 고아 예약치 (마커 TTL 만료는 resvsum 을 못 내림)
--   live    = SCAN 스냅샷에서 가져온 살아있는 마커 값의 합
--
-- 따라서 ``orphan = resvsum - live`` 가 정확히 누수 추정치다 — settle/release 는
-- 마커 기반 멱등으로 resvsum 도 함께 내리므로 이미 정산된 예약은 여기에 잡히지
-- 않는다. 이것이 usage 카운터를 **내리는** 것이 안전한 유일한 경로다:
-- 내리는 양이 고아 추정치로 한정되기 때문이다.
--
-- 퀴에센스 게이트: 스냅샷 시점의 resvsum(ARGV[2])과 현재값이 다르면 스캔과 이
-- eval 사이에 신규 예약/정산이 끼어든 것 — orphan 산정이 어긋나므로 고아
-- 되돌림과 resvsum 보정을 이번 틱 건너뛴다(리컨사일 창의 다음 틱이 잡는다).
-- usage 플로어는 어느 경우든 안전하므로 항상 적용한다.
--
-- usage 는 절대 ``db + live`` 아래로 내리지 않는다(양수 방향 플로어) — 활성
-- 유저의 cur > db 는 미flush 파이프라인 깊이라, 끌어내리면 반복적 fail-open
-- 창이 열린다.
--
-- KEYS[1] = usage 카운터 (budget:{scope}:{sid}:...)
-- KEYS[2] = resvsum 카운터
-- KEYS[3..] = 살아있는 예약 마커 키들 (SCAN 스냅샷 — 모두 같은 hash tag)
-- ARGV[1] = budget_usages.used_usd (문자열)
-- ARGV[2] = resvsum 스냅샷 (퀴에센스 검증용, 없으면 "0")
-- 반환: {live, orphan(음수면 raced/아래방향), usage_after}

local live = 0
for i = 3, #KEYS do
    local v = redis.call('GET', KEYS[i])
    if v then
        local n = tonumber(v)
        if n then live = live + n end
    end
end

local resvsum = tonumber(redis.call('GET', KEYS[2])) or 0
local snap    = tonumber(ARGV[2]) or 0
local orphan  = "raced"

if resvsum == snap then
    orphan = resvsum - live
    if orphan ~= 0 then
        -- resvsum 은 라이브 마커 합이 진실 — 양방향 보정.
        redis.call('SET', KEYS[2], tostring(live))
    end
    if orphan > 0 then
        -- 만료/고아 예약 추정치 되돌림 — usage 에만 남은 부분.
        redis.call('INCRBYFLOAT', KEYS[1], -orphan)
    end
end

local db     = tonumber(ARGV[1]) or 0
local target = db + live
local cur    = tonumber(redis.call('GET', KEYS[1])) or 0
if cur < target then
    redis.call('INCRBYFLOAT', KEYS[1], target - cur)
end

return {tostring(live), tostring(orphan), tostring(tonumber(redis.call('GET', KEYS[1])) or 0)}
