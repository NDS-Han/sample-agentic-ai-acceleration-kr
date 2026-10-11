-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
--
-- 예산 카운터 유실 복원 — 원자적 rebuild.
--
-- 배경(E2E S2-7): 예전엔 Python 이 EXISTS → DB 조회 → SET 하는 비원자 순서였다.
--   그 사이에 들어온 INCRBYFLOAT(예약/정산 델타)는 SET 에 덮어씌워져 소실되고,
--   그 요청의 settle 이 나중에 한 번도 더해지지 않은 예약치를 빼 과소 계상됐다.
--   반대 방향(델타가 먼저 키를 생성)도 있어 단순 EXISTS+SET 도 불완전하다.
--
-- 규칙: ``SET usage = ARGV[1](DB 실비+마커합) + 현재값`` — 키가 사이에 델타만
--   담고 생성됐으면 델타를 보존하고, 비어 있으면 베이스만 들어간다. 동시 rebuild
--   는 NX 락으로 한 명만 진행한다(두 rebuild 가 각각 베이스를 더해 이중 계상
--   되는 것을 막는다).
--
-- KEYS[1] = 사용 카운터 (budget:{scope}:{id}:...)
-- KEYS[2] = resvsum 카운터
-- KEYS[3] = rebuild 락 — 같은 hash tag 로 co-locate 시킬 것
-- ARGV[1] = 복원 베이스(DB 정산 실비 + in-flight 마커 합)
-- ARGV[2] = resvsum 베이스(= in-flight 마커 합)
-- Returns 1 = 복원 적용, 0 = 다른 rebuild 가 진행/완료

if redis.call('SET', KEYS[3], '1', 'NX', 'EX', 30) then
    local cur    = tonumber(redis.call('GET', KEYS[1])) or 0
    local cursum = tonumber(redis.call('GET', KEYS[2])) or 0
    redis.call('SET', KEYS[1], tonumber(ARGV[1]) + cur)
    redis.call('SET', KEYS[2], tonumber(ARGV[2]) + cursum)
    return 1
end
return 0
