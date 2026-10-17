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
-- KEYS[3] = pendingidx 해시 (budget:pendingidx:...)
-- KEYS[4..] = 스캔 스냅샷의 마커 키들 (웜업 창의 비인덱스 마커 커버용)
-- ARGV[1] = budget_usages.used_usd (문자열)
-- ARGV[2] = resvsum 스냅샷 (퀴에센스 검증용, 없으면 "0")
-- ARGV[3] = 마커 키 접두사 (budget:pending:{scope}:{id}:)
-- ARGV[4] = 인덱스 필드 접두사 필터 — client scope 는 "<client>:", 나머지 ""
--   (client 인덱스는 유저당 하나인데 카운터는 앱별 — 모든 앱의 필드를
--   합산하면 다른 앱의 예약이 이 카운터에 부푼다. rebuild 와 같은 계약,
--   R23 r5 M-1)
-- 반환: {live, orphan(음수면 raced/아래방향), usage_after}
--
-- ⚠️ live 는 eval 안에서 원자 계산한다 — Python 이 SCAN 한 마커 목록을
--    읽는 방식은 SCAN~스냅샷 사이에 들어온 예약을 고아로 차감했다
--    (실측: 라이브 예약이 usage 에서 빠져 HARD_BLOCK 우회, R23 r4 S-1).
--    인덱스 필드(post-warmup 진실)를 먼저 보고, 스캔이 넘긴 키 중 인덱스
--    미커버분(구버전 파드의 비인덱스 마커)만 추가한다.
local live = 0
local covered = {}
local dead = {}
local field_prefix = ARGV[4] or ''
for _, f in ipairs(redis.call('HKEYS', KEYS[3])) do
    local v = redis.call('GET', ARGV[3] .. f)
    local n = v and tonumber(v)
    if n then
        covered[f] = true
        if field_prefix == '' or string.sub(f, 1, #field_prefix) == field_prefix then
            live = live + n
        end
    elseif field_prefix == '' or string.sub(f, 1, #field_prefix) == field_prefix then
        -- 마커 부재 = 필드 사망의 확정 증거 — 실행은 퀴에센스 분기에서.
        -- 접두사 필터를 거는 이유: 다른 앱의 dead 필드를 지우면 그 앱의
        -- 행이 reconcile 되기 전 늦게 도착한 settle 이 생존 증거를 잃어
        -- 예약 복구를 못 한다(과대 계상, R23 r6 N-1). 각 앱 행이 자기
        -- 필드를 스윕하고, 사용 없는 앱은 인덱스 TTL(마커×2)이 거둔다.
        dead[#dead + 1] = f
    end
end
for i = 4, #KEYS do
    -- 스캔 시점 이후 만료된 마커는 GET nil 로 자연 제외.
    local f = string.sub(KEYS[i], #ARGV[3] + 1)
    if not covered[f] then
        local v = redis.call('GET', KEYS[i])
        local n = v and tonumber(v)
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
    if #dead > 0 then
        -- dead 인덱스 필드 정리 — 남아 있으면 settle 복구 분기의 HEXISTS
        -- 생존검증이 정리된 예약을 라이브로 오판해 다른 요청의 resvsum 을
        -- 차감한다(R23 r3 S-1). 고아 처리와 같은 틱에 묶어 스냅샷 어긋난
        -- 틱의 부분 정리를 막는다(R23 r4).
        redis.call('HDEL', KEYS[3], unpack(dead))
    end
end

local db     = tonumber(ARGV[1]) or 0
local target = db + live
local cur    = tonumber(redis.call('GET', KEYS[1])) or 0
if cur < target then
    redis.call('INCRBYFLOAT', KEYS[1], target - cur)
end

return {tostring(live), tostring(orphan), tostring(tonumber(redis.call('GET', KEYS[1])) or 0)}
