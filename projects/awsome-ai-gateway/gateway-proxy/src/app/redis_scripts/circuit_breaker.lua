-- Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

-- Record one call outcome into a per-second sliding window and decide whether to OPEN.
-- KEYS[1]=fail base (cb:{pmid}:fail) KEYS[2]=total base (cb:{pmid}:total) KEYS[3]=open flag (cb:{pmid}:open)
-- ARGV[1]=window_sec ARGV[2]=min_calls ARGV[3]=error_rate ARGV[4]=open_ms ARGV[5]=is_failure(1/0) ARGV[6]=now_ms
-- Returns 1 if circuit is (now) OPEN, else 0.
local window_sec = tonumber(ARGV[1])
local min_calls  = tonumber(ARGV[2])
local rate_thr   = tonumber(ARGV[3])
local open_ms    = tonumber(ARGV[4])
local is_failure = tonumber(ARGV[5])
local now_ms     = tonumber(ARGV[6])
local now_sec    = math.floor(now_ms / 1000)

if redis.call('EXISTS', KEYS[3]) == 1 then
  return 1
end

-- Window is [now_sec-(window_sec-1), now_sec] inclusive; bucket keys are second-scoped so EXPIRE resets are safe (bucket T only ever written during second T).
local total_key = KEYS[2] .. ':' .. now_sec
local fail_key  = KEYS[1] .. ':' .. now_sec
redis.call('INCR', total_key)
redis.call('EXPIRE', total_key, window_sec)
if is_failure == 1 then
  redis.call('INCR', fail_key)
  redis.call('EXPIRE', fail_key, window_sec)
end

-- 성공 기록은 열기 판정에 쓰이지 않으므로 창 합산을 건너뛴다 — 성공마다
-- window_sec × 2 회 GET 을 도는 것을 막는다(R22 r5 N4). 성공이 total 을
-- 채워 실패율 임계를 넘기는 경계 케이스(4F+1S)는 어차피 이 판정에서 열리지
-- 않고 다음 실패 기록에서 열린다 — is_failure 판정 자체가 실패 시점으로
-- 옮겨진 의도된 의미다.
if is_failure ~= 1 then
  return 0
end

local total = 0
local fails = 0
for i = 0, window_sec - 1 do
  local b = now_sec - i
  local t = redis.call('GET', KEYS[2] .. ':' .. b)
  local f = redis.call('GET', KEYS[1] .. ':' .. b)
  if t then total = total + tonumber(t) end
  if f then fails = fails + tonumber(f) end
end

-- 실패 기록일 때만 열기 판정을 한다 — 프로브 close 는 :open 만 지우고 초당
-- 버킷은 남으므로, 성공 기록에도 열기 판정을 하면 close 직후 첫 일반 요청이
-- 옛 실패 이력 때문에 회로를 즉시 재오픈해 프로브 회복이 ≈window 만큼
-- 죽는다(R22 r4 S1). 성공은 실패율을 내리기만 하므로 열 이유가 없다.
if is_failure == 1 and total >= min_calls and (fails / total) >= rate_thr then
  redis.call('SET', KEYS[3], '1', 'PX', open_ms)
  return 1
end
return 0
