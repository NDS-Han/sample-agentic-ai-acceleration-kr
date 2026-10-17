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
--    ``marker_prefix..<field>`` 마커들. Redis Cluster 의 스크립팅 계약상
--    KEYS 에 없는 키 접근은 허용되지 않지만, 마커 수는 가변이라 선언이
--    불가능하다. 모든 파생 키가 선언 키와 같은 {<id>} hash tag 를 공유해
--    같은 슬롯에 놓이도록 호출자가 보장한다(R22 r3 N9 — 태그 어긋나면
--    Cluster 에서 실행 시점 오류가 난다).
-- ARGV[1] = DB 정산 실비 (budget_usages/usage_logs)
-- ARGV[2] = 마커 키 접두사 (budget:pending:{scope}:{id}:)
-- ARGV[3] = 인덱스 필드 접두사 필터 (client scope 용 "", 또는 "claude-code:")
-- ARGV[4] = 호출자가 스캔한 pending 합 ('' = 인덱스 신뢰, Lua 내부 계산)
-- Returns 1 = rebuild 적용(센티넬 정리), 0 = 트리거 이미 해소됨

local lost    = redis.call('EXISTS', KEYS[1] .. ':lost')
local cur_raw = redis.call('GET', KEYS[1])
local cur     = tonumber(cur_raw)

-- 트리거: 센티넬 존재 OR 카운터 부재 OR 음수. 모두 아니면 이미 복원된 상태 —
-- 다른 파드가 먼저 적용한 것이라 no-op (S-5: 락 대신 조건부로 중복 차단).
if lost == 0 and cur_raw and cur and cur >= 0 then
    return 0
end

local live
if ARGV[4] ~= '' then
    -- 인덱스 웜업 창 — 구버전 마커가 인덱스에 없으므로 Python 스캔값을 신뢰.
    live = tonumber(ARGV[4]) or 0
else
    live = 0
    local marker_prefix = ARGV[2]
    local field_prefix  = ARGV[3] or ''
    local dead = {}
    for _, f in ipairs(redis.call('HKEYS', KEYS[3])) do
        local v = redis.call('GET', marker_prefix .. f)
        local n = v and tonumber(v)
        if n then
            if field_prefix == '' or string.sub(f, 1, #field_prefix) == field_prefix then
                live = live + n
            end
        else
            dead[#dead + 1] = f  -- 마커 TTL 만료 고아 필드 — 자기 청소
        end
    end
    if #dead > 0 then
        redis.call('HDEL', KEYS[3], unpack(dead))
    end
end

local target = tonumber(ARGV[1]) + live
if (not cur) or cur < target then
    redis.call('SET', KEYS[1], target)
    redis.call('EXPIRE', KEYS[1], 3888000)  -- 45d (deduct/reserve 와 동일)
end

-- resvsum 은 라이브 마커 합이 진실 — 양방향 보정 (reconcile 과 같은 규약).
redis.call('SET', KEYS[2], tostring(live))
redis.call('EXPIRE', KEYS[2], 3888000)

-- 센티넬 해제 — 다음 check 가 다시 트리거하지 않게.
redis.call('DEL', KEYS[1] .. ':lost')
return 1
