# R15 — Scheduler + Notification Worker 심층 리뷰 (2026-10)

리뷰어: kiro `claude-opus-5.5` (read-only) + 자체 코드 추적 검증.
대상: `admin-api/src/app/scheduler/*`, `notification-worker/src/worker/**`,
`analytics_repository.upsert_aggregation`.

## 발견 및 조치

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| 1 | HIGH | DB 장애 시 알림 전량 조용히 유실 — `NotificationBufferQueue.enqueue` 호출부가 없어 버퍼/drain이 사장 코드. 리졸버가 역할 단위로 DB 오류를 삼켜 `[]` 반환 → "수신자 없음" 폐기 | 리졸버가 `SQLAlchemyError` 를 다시 던지게 하고, 핸들러가 이를 `NotificationInfraError` 로 변환 → 리스너가 버퍼에 enqueue. 수신자 전원이 인프라 오류로 실패한 경우도 동일하게 전파(일부 실패는 pending 행 → sweeper 회수). HealthCheckTask 의 복구 drain 경로가 실제로 동작하게 됨 |
| 2 | HIGH | 보안 이벤트 폭주 무제한 — `event_detector` 가 임계 도달마다 새 event_id 로 발행(pod×worker 배가), SES 쿼터 소진 및 다른 알림 동반 지연 위험 | Redis `SET notif:cooldown:{type}:{sha1(정체성필드)} NX EX {cooldown}` dedup. 정체성 필드(source_ip/user_id/key_id/threshold_pct/service/provider/resource 등)가 없으면 dedup 생략. Redis 오류 시 fail-open. 인프라 실패로 이벤트를 되돌릴 땐 키 해제(드레인 재처리가 억제되지 않게). `notification_cooldown_seconds`(기본 600) |
| 3 | MEDIUM | 'pending' dead-claim 행 영구 미발송 — INSERT 후 send 전 crash/배포 취소/commit ack 유실 시 행만 남고, Pub/Sub 재발행이 없어 takeover 기회가 없음 | `PendingSweepTask` 신설: `UPDATE ... WHERE id IN (SELECT ... FOR UPDATE SKIP LOCKED)` 단일문으로 stale pending(300s+) 행을 원자 클레임 → `resend_log_entry` 로 재발송·확정 |
| 4 | MEDIUM | dead-claim takeover 가 read→write 라 두 replica 가 같은 stale 행 동시 발송 가능 | 클레임을 조건부 `UPDATE ... RETURNING` 단일문으로 원자화 — `status='pending' AND coalesce(last_attempt_at,created_at) < now()-300s` 매칭 시에만 인수 |
| 5 | MEDIUM | 'failed' 는 최종 상태 — SES 장애가 retry_executor(3회·수 초)를 넘기면 영구 미발송 | sweeper 가 600s 백오프 후 attempt_count<6 까지 재시도. 클레임 시 'pending' 으로 전이 + attempt_count+1 |
| 6 | MEDIUM | 느린 발송 1건이 채널 전체 읽기 차단(head-of-line) + half-open TCP 시 리스너가 조용히 귀머거리 | `get_message(timeout=60s)` 루프 + 타임아웃마다 `pubsub.ping()`(실패 시 Supervisor 재구독). 이벤트는 세마포어 바운드 태스크로 디스패치(`listener_max_inflight`=8), 종료 시 in-flight 15s 대기. `socket_keepalive`+`health_check_interval` 추가 |
| 7 | MEDIUM(latent) | ROI upsert UPDATE 분기가 생산성 지표 4개(code_acceptance/cost_per_accepted/lines_per_session/roi_index) 미갱신 — 첫 실행 값 고착 | update 분기에 4필드 복사 |
| 8 | LOW | 월말 뒷부분 사용량은 지난달 ROI 행에 반영 기회 없음(마지막 실행 ~23:45) | 새 달 1일엔 전월도 함께 재집계(upsert 멱등) |
| 9 | LOW | 스케줄러 크론이 컨테이너 TZ(UTC) — "새벽 3:30" 잡이 KST 점심 실행 | `AsyncIOScheduler`+`CronTrigger` 에 `REPORTING_TIMEZONE` 적용(cost-recorder와 동일 규약) |
| 10 | LOW | SIGTERM 시 `scheduler.shutdown()` 불도달 — 실행 중 잡이 그대로 kill | `loop.add_signal_handler(SIGTERM/SIGINT → loop.stop)` + finally 에서 shutdown |
| 11 | LOW | locale=en 에서 로케일 템플릿 없는 7개 타입이 한국어 비로케로 발송. ko budget 제목이 %/기간 누락으로 스레딩. [DS Gateway]/[LLM Gateway] 브랜딩 혼재 | 폴백 순서를 `{type}.{locale} → default.{locale} → {type} → default` 로 수정(로케일 일치 우선). ko 제목에 threshold_pct/period 추가. 비로케 제목의 [DS Gateway] → [LLM Gateway] 통일(본문은 이미 LLM Gateway) |
| 12 | LOW | 핸들러 로그에 수신자 이메일 평문 — ses_sender 만 마스킹 | `services/email_mask.py` 로 공용화, 핸들러 로그 전부 마스킹 |

추가 보강(리뷰 지적에 연계):
- 모든 잡에 `max_instances=1, coalesce=True, misfire_grace_time=300` — 중첩 실행/misfire 폭주 차단.
- 잡별 `pg_try_advisory_xact_lock` — 롤링 배포 구/신 pod 공존이나 실수로 늘어난 replica 의 동시 실행 차단.

## 배포 이력 정정 (선행)

R14 검증 라운드(V1–V3: PoolTimeout 503 재분류, JSON 오류행 복원, 후보 오류
마스킹 재발 방지)가 커밋 없이 워킹트리에 남아 이미지 `11934592` 에 미포함이었다.
본 라운드에서 `b38ea96d` 로 선커밋 후 재빌드/재배포.

## 수용 잔여

- 쿨다운은 "같은 정체성 알림 창 내 1통" 정책 — 서로 다른 키 만료 등 정체성
  필드가 다르면 각각 발송(의도). 페이로드에 정체성 필드가 없는 이벤트 타입은
  dedup 대상 아님.
- 버퍼는 인메모리(최대 1000, oldest-drop) — pod 재시작 시 버퍼분은 유실.
  durable spool 은 별도 작업.
- sweeper 재발송은 재클레임 간격 최소 300s — attempt_count 상한(6) 도달 행은
  로그로만 남음(영구 DLQ 테이블 없음).
- half-open 감지 heartbeat 는 최대 60s 지연 — 그 창의 유실은 pub/sub 특성상 불가피.
- ROI 월초 재집계는 1일 하루만 — 그 이후 도착하는 지연분은 미반영(비용 집계는
  별도 파이프라인이라 영향은 ROI 지표 한정).

## 검증 라운드 (수정분 재검토) — 8건 추가 수정

| # | 심각도 | 발견 | 조치 |
|---|---|---|---|
| V1 | HIGH | 템플릿 폴백 재배열이 기본 ko 로케일을 회귀 — 비로케 파일이 한국어라 `.ko` 없는 7개 타입이 default.ko 범용으로 뭉개짐 | `_candidates()` 로케일 분기 — ko: `{type}.ko → {type} → default.ko → default`, 비-ko: `{type}.{loc} → default.{loc} → {type} → default` |
| V2 | HIGH | sweeper 배치 클레임(50행)이 느린 발송 중 자기 stale 창을 넘겨 다른 replica 가 재클레임·이중 발송 | per-row 클레임(`LIMIT 1`) + 스윕 시간 예산(240s<300s) + 클레임 `last_attempt_at` 을 펜싱 토큰으로 `resend_log_entry` 에 전달, 발송 직전 조건부 UPDATE 재검증 |
| V3 | MED | 버퍼 drain 이 "다운→복구" 전이에서만 동작 — 체크 간격보다 짧은 DB 블립은 버퍼 적재만 되고 배출 없음 | DB 체크 성공 시 버퍼가 비어있지 않으면 매 주기 drain |
| V4 | MED | 쿨다운 키가 슬롯 커밋 전 취소/crash 시 잔류 — replica fan-out 재처리도 억제해 이벤트 유실 | handle() 의 claim 이후 구간을 `except BaseException` 으로 감싸 모든 비정상 종료에서 키 해제 |
| V5 | MED | ping 응답(pong)이 이벤트로 디스패치돼 분당 parse 에러 + ping 전송만으로는 half-open 미검지 | `type != "message"` 필터 복원 + 마지막 수신 시각 추적, 3×heartbeat 무응답 시 ConnectionError → 재구독 |
| V6 | MED | advisory xact lock 이 첫 커밋에서 해제 — 월초 전월 재집계 구간 무방비 | `pg_try_advisory_lock`(세션) + finally `pg_advisory_unlock` — 잡 전 구간 유지 |
| V7 | LOW | SIGTERM 시 `loop.stop` 이 shutdown 예약(call_soon)보다 먼저 실행돼 graceful shutdown 미동작 | 신호 핸들러가 코루틴으로 shutdown 예약 → 5s 유예 → loop.stop |
| V8 | LOW | sweeper 에 나이 상한·터미널 상태 없음 — 배포 시 과거 failed 전부 재발송, skipped 행 무한 회수, 상한 도달 행 영구 pending | `created_at > 24h` 한정 + `resend_skipped_*` 제외 + 상한 도달 pending 은 'abandoned' 확정 + non-retryable 발송 오류는 'rejected' 터미널 |

## 테스트

- admin-api: 958 pass / 50 skip (회귀 `test_r15_scheduler.py` +6)
- notification-worker: 77 pass (회귀 `test_r15_reliability.py` +17, dedup/listener
  갱신)
- gateway-proxy 회귀(`test_r14_fallback_review.py`): 7 pass (V-수정분 커밋 전 확인)
