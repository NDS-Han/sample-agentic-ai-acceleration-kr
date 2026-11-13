-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
--
-- 예산 카운터 유실 복원 — 조건부 트리거 + 양수 플로어 rebuild (R22 r2 M1/S5).
--
-- 역사:
--   1세대: Python EXISTS → DB 조회 → SET 비원자 — 사이의 INCRBYFLOAT 델타 유실.
--   2세대: SET = base + cur — "키 부재/음수" 트리거에서는 옳았으나, :lost
--          센티넬이 "키가 이미 base 에 포함된 값을 담고 있는" 상태에서도
--          발화하게 되면서 base 분량만큼 매번 이중 가산됐다.
--   3세대(본): 트리거가 아직 성립할 때만 실행 + ``cur < target`` 일 때만
--          올리는 플로어 — budget_reconcile.lua 와 같은 규약이다. 카운터를
--          절대 내리지 않으므로 활성 유저의 미flush 델타(cur > base)를
--          깎아 반복 fail-open 창을 여는 일이 없고, 동시 rebuild 도 자연히
--          멱등 수렴해 NX 락이 필요 없다.
--
-- pending 은 Lua 안에서 pendingidx 해시 + 마커 GET 으로 계산한다 — Python 이
-- 읽고 eval 하기까지의 창에 들어온 예약도 잡힌다(마커·인덱스·카운터는 모두
-- 같은 hash tag 슬롯). 단 롤링 배포/인덱스 웜업 창에는 구버전 파드의 마커가
-- 인덱스에 없으므로, 호출자가 ARGV[4] 에 스캔 합을 넘기면 그 값을 쓴다.
--
-- KEYS[1] = 사용 카운터 (budget:{scope}:{id}:...)
-- KEYS[2] = resvsum 카운터 (미정산 예약 합계)
-- KEYS[3] = pendingidx 해시 (budget:pendingidx:...)  — 마커와 같은 hash tag
--
-- ⚠️ 이 스크립트는 선언되지 않은 키도 접근한다 — ``KEYS[1]..':lost'`` 와
--    ``KEYS[1]..':lost_amt'`` (차감 실패분 누적), ``marker_prefix..<field>``
--    마커들. Redis Cluster 의 스크립팅 계약상
--    KEYS 에 없는 키 접근은 허용되지 않지만, 마커 수는 가변이라 선언이
--    불가능하다. 모든 파생 키가 선언 키와 같은 {<id>} hash tag 를 공유해
--    같은 슬롯에 놓이도록 호출자가 보장한다(R22 r3 N9 — 태그 어긋나면
--    Cluster 에서 실행 시점 오류가 난다).
-- ARGV[1] = DB 정산 실비 (budget_usages/usage_logs)
-- ARGV[2] = 마커 키 접두사 (budget:pending:{scope}:{id}:)
-- ARGV[3] = 인덱스 필드 접두사 필터 (client scope 용 "", 또는 "claude-code:")
-- ARGV[4] = 호출자가 스캔한 pending 합 ('' = 인덱스 신뢰, Lua 내부 계산)
-- Returns 1 = rebuild 적용(센티넬 정리), 0 = 트리거 이미 해소됨

-- NaN/±Inf 정규화 — tonumber('nan'/'inf') 는 truthy 라 `or` 폴백이 못 잡는다.
-- 카운터에 'nan' 이 들어가면 모든 비교가 false 가 돼 트리거는 서는데
-- `cur < target` 도 false 라 복구가 영구 불발하고, lost_amt 가 DEL 로
-- 조용히 버려진다(12차 리뷰 F1). NaN 은 "값 없음", Inf 는 0 으로 수렴시켜
-- 플로어 쓰기가 스스로 치유하게 한다.
local function fin(v)
    if type(v) ~= 'number' or v ~= v or v == math.huge or v == -math.huge then
        return nil
    end
    return v
end

-- WRONGTYPE 카운터는 GET 이 eval 을 죽여 아래 치유 SET 까지 못 간다 —
-- 문자열이 아니면 지워 정상 경로로 수렴(18차 F-2, pendingidx 와 같은 규약).
local function _fix_type(key, want)
    local t = redis.call('TYPE', key)
    if type(t) == 'table' then t = t['ok'] end
    if t ~= want and t ~= 'none' then redis.call('DEL', key) end
end
_fix_type(KEYS[1], 'string')
_fix_type(KEYS[2], 'string')
_fix_type(KEYS[1] .. ':lost_amt', 'string')

local lost    = redis.call('EXISTS', KEYS[1] .. ':lost')
local cur_raw = redis.call('GET', KEYS[1])
local cur     = fin(tonumber(cur_raw))

-- 트리거: 센티넬 존재 OR 카운터 부재 OR 음수. 모두 아니면 이미 복원된 상태 —
-- 다른 파드가 먼저 적용한 것이라 no-op (S-5: 락 대신 조건부로 중복 차단).
-- 비표준 형식(' 5' 등)도 트리거다 — fin()은 값으로 읽어 건강으로 보이지만
-- INCRBYFLOAT 가 거부해 매 deduct 가 커밋 전 크래시한다(18차 F5).
if lost == 0 and cur_raw and cur and cur >= 0
    and cur_raw == tostring(cur) then
    return 0
end

local live
if ARGV[4] ~= '' then
    -- 인덱스 웜업 창 — 구버전 마커가 인덱스에 없으므로 Python 스캔값을 신뢰.
    live = fin(tonumber(ARGV[4])) or 0
    -- 합계 오버플로 방어 — 거대 오염 항의 합이 +Inf 가 되면 'inf' 를 쓴다
    -- (14차 리뷰). 음수는 오염 — 0 수렴.
    live = fin(live) or 0
    if live < 0 then live = 0 end
else
    live = 0
    local marker_prefix = ARGV[2]
    local field_prefix  = ARGV[3] or ''
    local dead = {}
    -- WRONGTYPE 인덱스는 HKEYS eval 실패 → 매 트리거마다 같은 키에서 죽어
    -- rebuild 가 영구 미실행된다(17차 리뷰 F12) — 지워 재생을 연다.
    -- Lua TYPE 은 {ok='hash'} 테이블 — `~='hash'` 비교는 항상 참이라
    -- 정상 인덱스까지 지워지므로 ['ok'] 언랩이 필수.
    local _t3 = redis.call('TYPE', KEYS[3])
    if type(_t3) == 'table' then _t3 = _t3['ok'] end
    if _t3 ~= 'hash' then
        redis.call('DEL', KEYS[3])
    end
    for _, f in ipairs(redis.call('HKEYS', KEYS[3])) do
        -- WRONGTYPE 마커는 GET 이 죽는다 — 부재(dead)와 동치 수렴(18차 F-2).
        local ok_g, v = pcall(redis.call, 'GET', marker_prefix .. f)
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
            if field_prefix == '' or string.sub(f, 1, #field_prefix) == field_prefix then
                live = live + n
            end
        elseif field_prefix == '' or string.sub(f, 1, #field_prefix) == field_prefix then
            -- 접두사 필터는 reconcile 과 같은 규약 — client 인덱스는 앱 공용
            -- 해시라 다른 앱의 dead 필드를 지우면 그 앱 행의 reconcile 전에
            -- 도착한 settle 이 생존 증거를 잃는다(R23 r6 N-1, 13차 리뷰).
            dead[#dead + 1] = f  -- 마커 TTL 만료 고아 필드 — 자기 청소
        end
    end
    if #dead > 0 then
        -- unpack 은 ~8000 인자 상한 — 대량 dead 스윕이 스크립트를 죽이지
        -- 않게 청크로 나눈다(13차 리뷰).
        for i = 1, #dead, 500 do
            redis.call('HDEL', KEYS[3],
                       unpack(dead, i, math.min(i + 499, #dead)))
        end
    end
    -- 합계 오버플로는 0 수렴이면 target=db 만으로 과소 복원(스코프가 예산
    -- 전액 재취득, fail-open)이 된다 — MAXD 포화로 차단 방향 수렴(18차).
    live = fin(live) or 1.7976931348623157e308
    if live < 0 then live = 0 end  -- 오염 합산 방어(보수적 하한)
end

-- 비유한/비수치 원장('NaN'/'1e400'/'abc')은 0 으로 수렴하면 스코프가 이번
-- 달 예산 전액을 새로 받는 **fail-open** 이 된다(17차 리뷰 F3). 실사용치를
-- 모르는 오염은 소진으로 간주해 max-double 로 포화 — 행 수정까지 차단되고
-- target 클램프가 그대로 받는다. 행 부재는 호출자가 '0' 을 넘겨 정상 통과.
local db = fin(tonumber(ARGV[1])) or 1.7976931348623157e308
if db < 0 then db = 0 end  -- DB used_usd 음수 = 오염 — target 음수화 방지
local target = db + live
-- db+live 오버플로는 +Inf — 'inf' 카운터는 영구 fail-closed + 매 요청
-- rebuild 폭풍이므로 max-double 로 클램프(차단 유지·프로브 건강, 15차).
target = math.min(target, 1.7976931348623157e308)

-- 차감 실패분 누적 — 원장(budget_usages)은 워커 배치라 rebuild 시점에 실패분이
-- 아직 미반영일 수 있다. 그 상태로 센티넬만 지우면 유실분이 기간 내내 남으므로
-- cur+lost_amt 도 플로어 후보로 올린다 — 이후 deduct 가 계속 cur 에 누적되므로
-- cur+lost_amt 가 정확한 복원값이다 (db+live 가 작게 읽힐 창을 덮는다).
local lost_amt = fin(tonumber(redis.call('GET', KEYS[1] .. ':lost_amt'))) or 0
if lost_amt > 0 then
    -- (cur or 0)+lost_amt 도 오버플로 가능 — 같은 클램프를 max 이후에도.
    target = math.min(math.max(target, (cur or 0) + lost_amt),
                      1.7976931348623157e308)
end

if (not cur) or cur < target then
    redis.call('SET', KEYS[1], target)
    redis.call('EXPIRE', KEYS[1], 3888000)  -- 45d (deduct/reserve 와 동일)
elseif cur_raw ~= tostring(cur) then
    -- 플로어는 안 올려도 되지만 형식이 비표준이면 INCRBYFLOAT 가 거부해
    -- 매 deduct 가 크래시→센티넬→no-op rebuild 루프로 영구 과소(18차 F5).
    -- 값 보존 표준형 재기록 + TTL-less 키에는 TTL 부여.
    redis.call('SET', KEYS[1], tostring(cur), 'KEEPTTL')
    if redis.call('TTL', KEYS[1]) < 0 then
        redis.call('EXPIRE', KEYS[1], 3888000)
    end
end

-- resvsum 은 라이브 마커 합이 진실 — 양방향 보정 (reconcile 과 같은 규약).
redis.call('SET', KEYS[2], tostring(live))
redis.call('EXPIRE', KEYS[2], 3888000)

-- 센티넬 해제 — 다음 check 가 다시 트리거하지 않게. ⚠️ 롤링 배포 중 구버전
-- 스크립트는 ':lost' 만 지우므로 lost_amt 잔여가 남을 수 있다 — 다음 rebuild
-- 가 cur+lost_amt 로 한 번 더 얹어 일시 과대 계상될 수 있으나, 최초 신버전
-- rebuild 때 함께 정리돼 자기수정된다(폭은 실패분 누적치, 기간은 배포 창).
redis.call('DEL', KEYS[1] .. ':lost', KEYS[1] .. ':lost_amt')
return 1
