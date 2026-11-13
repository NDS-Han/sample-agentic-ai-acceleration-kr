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
-- NaN/±Inf 정규화 — 'nan' 마커·카운터는 tonumber 가 truthy NaN 을 반환해
-- `or` 폴백이 못 잡고, live/resvsum 에 섞이면 `SET tostring(live)` 가 'nan'
-- 을 카운터에 다시 쓴다(12차 리뷰 F1). 비유한 값은 nil(마커는 dead, 카운터는
-- `or 0` 폴백)으로 수렴시킨다.
local function fin(v)
    if type(v) ~= 'number' or v ~= v or v == math.huge or v == -math.huge then
        return nil
    end
    return v
end

local live = 0
local covered = {}
local dead = {}
local field_prefix = ARGV[4] or ''
-- WRONGTYPE 카운터는 GET 이 eval 을 죽여 치유 SET 까지 못 가고 매 틱 같은
-- 키에서 죽는다(18차 F-2). 문자열이 아니면 지워 아래 플로어/보정이 정상
-- 경로로 수렴하게 한다(pendingidx 와 같은 규약 — Lua TYPE 은
-- {ok='string'} 테이블이라 언랩 필수).
local function _fix_type(key, want)
    local t = redis.call('TYPE', key)
    if type(t) == 'table' then t = t['ok'] end
    if t ~= want and t ~= 'none' then redis.call('DEL', key) end
end
_fix_type(KEYS[1], 'string')
_fix_type(KEYS[2], 'string')
-- WRONGTYPE 인덱스(문자열/리스트가 같은 경로에 심어진 경우)는 HKEYS 가
-- eval 실패로 죽어 호출측이 그 행을 매 틱 스킵한다 — resvsum 드리프트가
-- 영구 미청소(17차 리뷰 F12). hash 가 아니면 재생 불가 데이터이므로 지워
-- 다음 reserve 의 HSET 이 정상 해시를 다시 만들게 한다.
_fix_type(KEYS[3], 'hash')
for _, f in ipairs(redis.call('HKEYS', KEYS[3])) do
    -- WRONGTYPE 마커는 GET 이 죽는다 — 부재와 동치로(dead) 수렴(18차 F-2).
    local ok_g, v = pcall(redis.call, 'GET', ARGV[3] .. f)
    if not ok_g then v = nil end
    local n = v and fin(tonumber(v))
    if v and (not n or n < 0) then
        -- 마커는 살아 있는데 값이 오염('nan'/'abc'/음수)된 경우 — 인덱스
        -- 필드의 예약액 사본으로 복구를 시도한다. 그냥 dead 처리하면
        -- 마커 만료 뒤 settle 복구의 생존 증거·예약액이 같이 지워진다
        -- (13차 리뷰).
        n = fin(tonumber(redis.call('HGET', KEYS[3], f)))
    end
    -- 음수 값은 오염(마커든 인덱스 사본이든) — live 에 섞이면
    -- orphan=resvsum-live 가 부풀어 usage 를 무한정 배수 배출한다
    -- (13차 리뷰 MAJOR).
    if n and n < 0 then n = nil end
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
        local ok_g, v = pcall(redis.call, 'GET', KEYS[i])
        if not ok_g then v = nil end
        local n = v and fin(tonumber(v))
        if n and n < 0 then n = nil end  -- 음수 마커 오염 방어(위와 동일)
        if n then live = live + n end
    end
end
-- 합계 자체의 오버플로/음수 방어 — ≥1e308 항들의 합은 +Inf 가 돼
-- `live < 0` 를 빠져나가 'inf' 를 resvsum/usage 에 쓴다(14차 리뷰).
-- 0 수렴은 orphan=resvsum-0=resvsum 으로 실사용을 그만큼 배수 배출해
-- 영구 과소 계상이 된다(18차 리뷰) — MAXD 포화면 resvsum=MAXD 로 남고
-- 다음 틱의 orphan 드레인→음수 카운터→프로브 rebuild 로 수렴한다.
live = fin(live) or 1.7976931348623157e308
if live < 0 then live = 0 end

local resvraw = fin(tonumber(redis.call('GET', KEYS[2])))
local resvsum = resvraw or 0
local snap    = fin(tonumber(ARGV[2])) or 0
if resvsum < 0 then resvsum = 0 end
if snap    < 0 then snap    = 0 end
local orphan  = "raced"

if resvsum == snap then
    orphan = resvsum - live
    -- resvraw=nil(저장값 오염/부재)도 치유 — orphan=0 이면 기존 게이트가
    -- 쓰기를 건너뛰어 'nan' resvsum 이 영구 잔존하고, 그 키에서 settle /
    -- reserve 의 INCRBYFLOAT 가 매번 죽는다(15차 리뷰).
    if orphan ~= 0 or not resvraw then
        -- resvsum 은 라이브 마커 합이 진실 — 양방향 보정.
        -- SET 은 TTL 을 지우므로 다른 쓰기와 같은 45d 를 다시 건다(14차 리뷰).
        redis.call('SET', KEYS[2], tostring(live))
        redis.call('EXPIRE', KEYS[2], 3888000)
    end
    if orphan > 0 then
        -- 만료/고아 예약 추정치 되돌림 — usage 에만 남은 부분.
        -- INCRBYFLOAT 대신 SET: 카운터가 'nan' 등 비수치면 INCRBYFLOAT 가
        -- 매 틱 오류를 내 그 스코프의 교정 루프가 영구 정지한다(13차 리뷰).
        -- 같은 스크립트 원자 실행이라 의미는 동치.
        local cur0 = fin(tonumber(redis.call('GET', KEYS[1]))) or 0
        redis.call('SET', KEYS[1], tostring(cur0 - orphan))
        redis.call('EXPIRE', KEYS[1], 3888000)
    end
    if #dead > 0 then
        -- dead 인덱스 필드 정리 — 남아 있으면 settle 복구 분기의 HEXISTS
        -- 생존검증이 정리된 예약을 라이브로 오판해 다른 요청의 resvsum 을
        -- 차감한다(R23 r3 S-1). 고아 처리와 같은 틱에 묶어 스냅샷 어긋난
        -- 틱의 부분 정리를 막는다(R23 r4).
        -- unpack 은 ~8000 인자 상한 — 대량 dead 스윕은 청크로 나눈다.
        for i = 1, #dead, 500 do
            redis.call('HDEL', KEYS[3],
                       unpack(dead, i, math.min(i + 499, #dead)))
        end
    end
end

-- 비유한/비수치 원장은 0 수렴 시 스코프가 예산 전액을 새로 받는 fail-open
-- (17차 리뷰 F3). 소진으로 간주해 max-double 포화 — 행 수정까지 차단.
local db     = fin(tonumber(ARGV[1])) or 1.7976931348623157e308
if db < 0 then db = 0 end  -- DB used_usd 음수 = 오염 — target 이 음수가
                           -- 되면 플로어가 아무것도 복구하지 못한다.
local target = db + live
-- db+live 오버플로는 +Inf — 'inf' 를 쓰면 카운터가 영구 fail-closed 되고
-- isfinite 프로브가 rebuild 를 매 요청 다시 태워 폭풍이 된다(15차 리뷰).
-- max-double 로 클램프하면 차단은 유지하되 프로브는 건강으로 본다.
target = math.min(target, 1.7976931348623157e308)
-- (not cur) 도 써야 'nan'/'abc' 카운터가 target=0 일 때도 덮어써 치유된다
-- — cur=0 폴백은 0<0 이라 쓰기를 건너뛰고 오염 값이 남는다(14차 리뷰).
local cur_raw = redis.call('GET', KEYS[1])
local cur     = fin(tonumber(cur_raw))
if (not cur) or cur < target then
    -- INCRBYFLOAT(target-cur) 와 동치(같은 스크립트 원자 실행)이면서 'nan'
    -- 같은 비수치 카운터도 덮어써 치유된다 — INCRBYFLOAT 는 그 키에서
    -- 매 틱 오류를 낸다(12차 리뷰 F1 후속, 13차). TTL 은 다른 쓰기와
    -- 같은 45d.
    redis.call('SET', KEYS[1], tostring(target))
    redis.call('EXPIRE', KEYS[1], 3888000)
elseif cur_raw ~= tostring(cur) then
    -- ' 5' 등은 fin()은 읽지만 INCRBYFLOAT 가 거부 — 플로어가 안 올려도
    -- 형식은 표준형으로 재기록해 다음 deduct 를 지킨다(18차 F5).
    redis.call('SET', KEYS[1], tostring(cur), 'KEEPTTL')
    if redis.call('TTL', KEYS[1]) < 0 then
        redis.call('EXPIRE', KEYS[1], 3888000)
    end
end

return {tostring(live), tostring(orphan), tostring(fin(tonumber(redis.call('GET', KEYS[1]))) or 0)}
