# 워커 컴포넌트 최종 리뷰 (cost-recorder / notification / pricing-lambda / policy-helper)

> 상태: 독립 리뷰 완료 + 확정 결함 수정·테스트 추가
> (policy-helper 52 tests, pricing-lambda 7, cost-recorder 58+12skip, notification 91+4skip).
> 전 라운드(R15~R27) 경화 흔적이 축적된 상태 — 잔여는 대부분 관측성/경계조건.

## 핵심 계약

- **cost:stream 소비**: 커밋→XACK(at-least-once). replay 방어 = request_id UNIQUE +
  RETURNING 기준 가산 + 배치내 dedup + tx내 SELECT 필터. 고아 PEL = XAUTOCLAIM(300s
  idle, 60s 주기). poison = 디코드 실패 즉시 ACK + 영구 SQLSTATE(22/23) 행 스킵 +
  head-streak 120s dead-letter; 스키마 오류(42)는 전파→PEL 보존.
- **중복 발송 방어**: (event_id,event_type,recipient_email) 유니크 슬롯 + Lua NX
  쿨다운(600s, 동일 event_id fan-out 통과) + dead-claim 인수(300s) + sweeper 펜싱.
- **유실 허용선(문서화)**: Pub/Sub 무재생 — publish 시점 구독자 0이면 임계 알림 유실
  (WARN+메트릭); 재전달분의 카운터/알림은 replay 스킵 — 익일 집계가 정정.
- **종료**: cost-recorder = request_stop + 협조창 후 cancel; notification =
  stop_all(18s) + 인플라이트 15s + 종료 drain 10s.

## 발견·수정된 결함

| # | 심각도 | 내용 |
|---|--------|------|
| 1 | MEDIUM | policy-helper `_NoRedirect` 무테스트 — 30x 회귀 시 VK 유출 무방비. → 리다이렉트 서버 대상 회귀 테스트 추가(target 미수신+미전달 검증) |
| 2 | MEDIUM | `handlers/base.py` 수신자 전원 비-인프라 실패 시 `status="success"` 오집계 + 쿨다운 잔류 → 알림 계열 무음 사막화. → 전원 실패 시 `status="failed"` + `all_recipients_failed_non_infra` 로그(쿨다운은 유지 — poison 재시도 폭풍 방지) |
| 3 | MEDIUM | `str(exc)` 로깅에 SQLAlchemy 바운드 파라미터(수신자 이메일·payload) 노출 — `dedup_slot_claim_failed`·`db_write_failed`·`event_buffered_infra_error` 3곳 → `mask_emails_in_text` 적용 |
| 4 | MEDIUM-LOW | pricing-lambda `urlopen` 리다이렉트 추종(https→http 다운그레이드로 https 강제 우회) + `resp.read()` 무제한 → `_NoRedirect` opener + 16MiB 상한. 테스트 스텁 `_OPENER.open` 패치로 전환 + 리다이렉트 회귀 테스트 |
| 5 | MEDIUM-LOW | Redis mode 감지 probe 가 TLS/timeout 미적용 — ElastiCache TLS 에서 평문 probe 실패 → `standalone` 오감지 → 클러스터면 모든 스트림 명령 MOVED 영구 크래시. → 두 워커 모두 probe 에 TLS/connect+socket timeout 적용 + 실패 경고 로그 |
| 6 | LOW | sweeper 클레임 스캔 O(N×테이블) — `(status,created_at)` 부분 인덱스 권장(마이그레이션 후속) |
| 7 | LOW | `unsubscribe` 실패 시 `aclose` 누락 — 재시작 주기당 pubsub 연결 orphan. → 두 리스너 모두 unsubscribe 실패를 삼키고 aclose 보장 |
| 8 | LOW | `internal_api_sender` timeout 하드코딩(10s)·3xx→영구 rejected — 설정 튜너블/명시 처리 권장 |
| 9 | LOW | 발송 성공 후 finalize commit 실패 → sweeper 재발송 중복 메일(설계상 한계, 하트비트로 축소 가능) |
| 10 | LOW | `channel_listener.py` 이벤트 처리 실패 로그의 `str(exc)` 미마스킹 잔여 — diff 스윕에서 발견, `mask_emails_in_text` 적용 |
| 11 | LOW | pricing-lambda `read(MAX+1)` 절단본을 그대로 `json.loads` — 명시 길이 검사로 오류 메시지 명확화(diff 스윕) |

## 잔여 리스크(계약상 허용·문서화 확인)

- `_PER_APP_CLIENTS` 수동 동기(게이트웨이와 이중 유지) — 어긋나면 앱별 예산 행 부재/고아 행.
- `period`/`date` 형식 미검증 — 게이트웨이 페이로드 신뢰 전제.
- 쿨다운 키는 처리 결과 무관 유지(수신자 0·config off 제외) — 동일 정체성 억제는 의도.
- MAXLEN~ 백스톱의 미배달 entry 절단 — `stream_gap_detected` 알람이 유일한 신호.
- 버퍼(1000) 초과분·종료 중 재큐 잔여는 메모리 유실.
- 스케줄러/startup backfill은 shutdown 대기 대상 아님 — 멱등 재실행으로 상쇄.
