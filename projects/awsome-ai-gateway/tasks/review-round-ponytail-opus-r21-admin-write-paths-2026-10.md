# R21 — admin-api 관리 쓰기 경로 리뷰 (kiro opus55 × ponytail)

범위: R13(analytics 읽기)·R16(인증 체인)이 덮지 못한 **관리자 mutation 표면** —
`routers/` 의 users/apps/budgets/keys/service_tokens/internal/monitoring/cli/
audit_reconcile/settings/routing/rate_limits + 호출 서비스군
(budget/key/user_team/service_token/cognito_sync/oidc/effective_policy/
rate_limit/allowed_client_scope).

리뷰어: `kiro-cli` + `opus55` 에이전트(read-only) — 매 라운드 diff + 스코프 파일
전문을 실측해 발견, 수정분은 다음 라운드에서 재검증.

## 라운드 1 — NOT CONVERGED (M4 + S11 + N8)

### Material

- **M1 예산 `alert_thresholds` 미전송 시 DB에 기본값 저장**
  `SetBudgetRequest` 생략 → resolved(기존값 보존)로 Redis/audit은 쓰는데
  `BudgetConfig` 생성자엔 `data.alert_thresholds`(스키마 기본 `[80,90,100]`)가
  들어감. migration 0041 이후 DB가 source-of-truth라 Redis 만료/재시작 후
  임계값 소실. → 3개 생성자(팀/유저/앱별) 모두 `sorted(set(alert_thresholds))`
  resolved 로컬 사용.
- **M2 감사 필드 오버플로로 배치 유실**
  클라이언트 제어 `x-request-id`/path resource_id가 컬럼 폭 초과 → 최대 100행
  배치 INSERT 통째 실패. → `log()`에서 컬럼 폭 클립(actor_role 32, action 128,
  resource_type 64, resource_id 256, result 32, ip 45, request_id 128) +
  벌크 실패 시 단건 폴백.
- **M3 `bodylog:enabled` 영구 Redis 키**
  OFF 저장 중 Redis SET 실패하면 TTL 없는 `"1"`이 무한 잔존 → 게이트웨이가
  DB(OFF)와 무관하게 본문 로깅 지속. → admin-api 미러와 gateway 재수화 양쪽에
  `BODY_LOG_REDIS_TTL_SECONDS=300` TTL 부여(자기치유).
- **M4 동일 팀 재저장이 이관 부수효과 발화**
  `transfer_user(user→같은 팀)`이 개별 예산·USER rate-limit 비활성화를 실행.
  → old==new 조기 반환(DB 조회 없이 `_to_user_response`), 실 이관만 팀 존재
  검증(없으면 FK 500 대신 404).

### Should-fix

- **S1 리더 상호 예산 배정** — 같은 팀 리더 A→B 배정으로 D-13 self-allocation
  우회. → 유저 예산 set/delete + 앱별 set/delete + 배정 목록 경로에
  대상 role(TEAM_LEADER/ADMIN) 거부(`leader_target_forbidden`).
- **S2 앱 클라이언트 revoke 레이스** — `allowed_clients` read-modify-write를
  `session.get(..., with_for_update=True)`로 직렬화.
- **S3 서비스 토큰 수명/계보** — `expiry_days` ge=1/le=365, `name` 1~128,
  `rotated_from` 응답 노출, `revoke()`가 `rotated_from` 사슬 연쇄 폐기 +
  grace 중 토큰도 now로 덮어씀. rotate 수명은 구 토큰 잔여로 상한.
- **S4 서비스토큰 감사 식별자** — svc- 토큰은 합성 user_id 공유 →
  `log(service_token_id=...)`로 `changes._service_token_id` 기록(49 호출부).
- **S5 감사-비즈니스 트랜잭션 동거** — `log(session)`이 있으면
  `session.add(AuditLog)`로 커밋과 동거(롤백 시 감사도 롤백). 실패 경로
  기록(열람 감사 등)은 `queue_only=True`로 큐 유지.
- **S6 force-reauth 유효 EXPIRED 누락** — `list_revocable_for_team`
  (ACTIVE + expires_at 미도래 EXPIRED) 신설 + 커서 페이지네이션(500/페이지,
  1000 상한 제거).
- **S7 수동 role/team 덮어쓰기** — `auth.users.team_source` 컬럼
  (migration 0048, 'sync'|'manual'). `transfer_user` 기본 'manual',
  OIDC/sync 경로 'sync'. manual이면 sync/로그인이 팀을 되돌리지 않음.
  ADMIN 강등은 `admin_mapping_configured()`(ADMIN_GROUPS/EMAILS 설정)일 때만.
  stale-team 클린업은 `COGNITO_SYNC_DEACTIVATE_MISSING` 플래그 뒤로 +
  manual 멤버 스킵.
- **S8 OIDC 이관 savepoint** — `_upsert_user`의 `transfer_user`를
  `session.begin_nested()`로 감싸 부분 이관(예산만 비활성) 방지.
- **S9 무제한 배치** — budget 스키마 max_length/min_length +
  `retry_failed` LIMIT 500 + retry_count<100.
- **S10 단일유저 오프보딩 오판** — `admin_get_user` 미스 ≠ 삭제
  (federated Username≠sub). 비활성화 전 `list_users(sub=...)`로 풀 잔존
  확인, 확인 불가면 fail-closed. 비-OIDC/이미-비활성은 확인조차 생략.
- **S11 멀티 그룹 팀 플립** — `sync_group`은 기존 유저 팀을 덮지 않음
  (`preserve_existing_team`); `_effective_role`은 실제 적용될 팀(kept_team)
  기준으로 판정.

### Nits

- **N1** 라우터 path/query `str`→`uuid.UUID` 변환(422), `uuid.UUID(uuid_obj)`
  재파싱 제거. dashboard `team_id="all"` 센티널은 str 유지.
- **N2** `uq_teams_dept_name`/`uq_departments_org_name`(migration 0045)를
  `_CONFLICT_CONSTRAINTS`에 추가 + create 스키마 `min_length=1`.
  (응답 모델은 기존 빈 이름 데이터 직렬화 보존을 위해 완화 유지)
- **N3** `request.headers.get("x-request-id")` →
  `request.state.request_id` 우선(미들웨어 생성값 커버), 13 라우터.
- **N4** `audit_reconcile` 본문 열람 실패 경로(400/502/503)도 감사 기록 —
  `_audit_body_view` 헬퍼로 성공/실패/오류 통일.
- **N5** `key_service.issue_key`의 Redis SET(key:vk/key:cache:vk/team:vk_hashes)
  3건을 `defer_redis_write_until_commit`로 커밋 후로 지연 — 롤백 시 유령 키
  인증 차단. ORM 속성은 지역 캡처.
- **N6** `set_for_team`의 "ACTIVE 검증" 주석과 구현 일치 — 비활성 별칭 거부.
- **N7** `/internal/test/issue-key` — JSON/숫자 입력 검증(400), ADMIN 대상
  발급 차단.
- **N8** `unset_team_leader` 스테일 포인터 재지정에도 감사 행
  (`stale_pointer_fix: true`).

## 라운드 1 검증

- admin-api `tests/` 전체 **981 passed, 50 skipped**(외부 Redis/PG 필요분)
- 신규 회귀 `tests/regression/test_r21_admin_write_paths.py` 11건 —
  클립/단건 폴백/세션 바인딩/queue_only/스키마 경계/연쇄 폐기/grace 덮어쓰기/
  비활성 별칭 거부
- 기존 테스트 갱신: force-reauth 목(`list_revocable_for_team`), Cognito 삭제
  확인 목(`list_users`), ADMIN 강등 매핑 설정 명시, stale-team 플래그 활성화,
  제약-마이그레이션 테스트를 전체 versions 디렉터리 스캔으로
- 수정 중 잡힌 결함: `User.team` relationship 실수 삭제 복원,
  `AppUserRef.user_id` 스키마 계약(str) 복원, 감사 호출 들여쓰기 정규화,
  audit_reconcile `log_start` 누락 복원

## 라운드 2 — NOT CONVERGED (M3 + S3 + N급)

라운드 1 수정분 재검증 결과:

- **M-A(기각)** — "커밋 후 `session.add`가 유실될 수 있다"는 주장은
  SQLAlchemy 2.0.49 실측으로 기각: `session.add()`는 autobegin을 발화해
  `in_transaction()`이 True → CommittingRoute가 정상 커밋. 다만 명시 커밋 후
  추가 감사를 쓰는 경로는 `queue_only`/세션 바인딩 정책으로 정리.
- **M-B 감사 `changes`의 Decimal 비직렬화** — `budget_service`의
  allocations/auto-downgrade 감사 페이로드가 `model_dump()` 그대로(JSONB에
  Decimal). → `model_dump(mode="json")` 2곳.
- **M-C 서비스토큰 폐기 연쇄가 사망 조상에서 단절** — grace 만료로 이미
  `revoked_at <= now`인 토큰을 만나면 자손 탐색 자체를 건너뛰어, self-rotate된
  후속 토큰이 살아남음. → 자손 탐색은 항상 수행하고 쓰기(재폐기)만 스킵,
  연쇄 대상 id를 감사 `changes._service_token_cascade`에 기록.
- **S-1 단건 폴백의 비행 오류 재시도 폭풍** — 연결/풀 오류에도 100회 단건
  재시도. → 폴백은 `DataError`/`IntegrityError`(행 단위 독 행)에만.
- **S-2 OIDC 이관 savepoint 롤백 후 `MissingGreenlet`** — expire된 `user`
  속성 접근. → 스칼라(`user_id`, `is_active`, `team_id`)를 savepoint 전에
  지역 캡처, 실패 시 refresh 후 재읽기.
- **S-3 같은 팀 재저장이 `team_source` 미갱신** — M4 no-op 경로가 수동
  고정을 저장하지 않음. → no-op 분기에서 `team_source="manual"` 설정.
- **N급** — 스테일 주석/docstring 정정, `/internal/test/issue-key`에
  NaN/무한·timedelta 오버플로 검증 추가, `TransferUserRequest.team_id` 등
  body UUID 필드 UUID 타입화(라우터 재파싱 제거), `AllocateBudgetItem.user_id`
  UUID화, body-view 감사는 성공=세션 바인딩/실패=queue_only 정합,
  `retry_failed`에 `FOR UPDATE SKIP LOCKED`(동시 워커 경합 차단).

### 라운드 2 검증

- admin-api `tests/` 전체 **994 passed, 50 skipped**
- 회귀 테스트 +2건(총 13): 인프라 오류는 단건 폴백하지 않음(S-1 가드),
  사망 조상 너머의 후속 토큰 폐기(M-C 시나리오)
- 테스트 수정: 독 행 예외를 `RuntimeError`→`DataError`로(폴백 조건 정합)

## 라운드 3 — NOT CONVERGED (M1 + S4 + N5)

- **M1(부분 기각)** — "session.add() 가 autobegin 하지 않아 세션-바운드 감사가
  유실" 주장은 설치된 SQLAlchemy 실측으로 기각(`add` 후 `in_transaction()=True`,
  r2 M-A 재소송). 다만 방어 차원에서 채택: CommittingRoute·get_db_session 이
  `in_transaction()` 이 False 여도 `session.new/dirty/deleted` 가 있으면
  `flush()` 로 트랜잭션을 열어 커밋 대상으로 만든다.
- **S1 지연 Redis SET 을 응답 전에 완료 보장** — `defer_redis_write_until_commit`
  에 `surface_failure` 추가, 발사된 태스크를 `session.info` 에 기록해
  CommittingRoute/get_db_session 이 `drain_deferred_redis_writes` 로 응답 전에
  await. `issue_key` 의 `key:vk:{hash}` SET 은 `surface_failure=True` — 실패가
  조용한 dead credential(24h 401)을 만드는 걸 막고, 실패 시 500 으로 바뀌어
  클라이언트가 재시도한다(orphan DB 행은 무해). 내부 커밋 엔드포인트도 커버.
- **S2 transfer_user·_upsert_one_user 행 잠금** — `session.get(...,
  with_for_update=True, populate_existing=True)` 로 동시 관리자 이관 vs OIDC/
  sync 경합에서 스테일 객체의 manual 가드 오판 방지. populate_existing 이
  미플러시 변경을 덮어쓰므로 잠금 전 `session.flush()`. 잠긴 행이 'manual'
  이면 sync 호출은 no-op. 테스트 더블 세션은 `isinstance(session, AsyncSession)`
  가드로 기존 목 계약 유지.
- **S3 rate-limit usage API 회귀** — 라우터가 `uuid.UUID` 를 넘기는데 서비스가
  `uuid.UUID(scope_id)` 를 다시 호출해 AttributeError → 트렌드가 항상
  unavailable, tracked 항상 None. → 라우터에서 `str(scope_id)` 전달.
- **S4 서비스토큰 폐기 상향 연쇄** — `rotated_from` 을 따라 부모로도 걷는다.
  유출 토큰이 self-rotate 된 뒤 자식만 폐기하면 grace 창의 부모가 최대 24h 더
  인증된다 — 양방향 BFS + `seen` 으로 사슬 전체를 닫는다.
- **N1** audit `json.dumps` 에 `allow_nan=False` — NaN 이 bare token 으로 JSONB
  거부를 유도해 500 을 내는 경로 차단(폴백 `{"_unserializable": true}`).
- **N2** `TransferUserRequest.team_source` 필드 추가(`'manual'|'sync'`) —
  같은 팀 재저장의 수동 고정을 감사하고, 'sync' 로 핀 해제가 가능해진다.
- **N3** force-reauth 가 EXPIRED-but-Redis-live 키를 폐기할 때 per-key
  `key_revoked` 메일이 멤버당 여러 통 → `revoke_key(notify=)` 추가, EXPIRED
  키는 알림 생략(멤버당 최대 1통 불변식 복원).
- **N4** 팀 허용 모델 — 이미 목록에 있는 별칭은 재전송 시 ACTIVE 검증을
  건너뛴다(등록 후 비활성화된 별칭이 목록 편집 자체를 막던 결함).
- **N5** `/internal/test/issue-key` — `email`/`display_name` 비문자열 입력이
  DB 오류로 500 → isinstance 검증 400.

### 라운드 3 검증

- admin-api `tests/` 전체 **996 passed, 50 skipped**
- 회귀 +3건(총 16): 기존 별칭 재전송 허용(N4), 같은 팀 재저장 pin 감사(N2),
  인프라 오류 비폴백(S-1, r2)
- 수정 중 잡힌 결함: `drain_deferred_redis_writes`/`_on_commit` 의
  `session.info` 접근이 AsyncMock 세션에서 TypeError — dict isinstance 가드

## 라운드 4 — NOT CONVERGED (M2 + S3 + N3)

라운드 3 신규 메커니즘의 재검증에서 r3 회귀 2건 발견:

- **M1 관리자 unpin 불가** — `team_source=='sync' and user.team_source=='manual'`
  가드가 OIDC/sync 뿐 아니라 관리자의 `team_source:"sync"` 요청(핀 해제)까지
  조용히 200 무시. → `from_sync: bool` 로 caller 분리(oidc 호출부만 True).
- **M2 500→재시도가 dead credential 반환** — surfaced SET 실패가 500 이 된 뒤
  빠른 재시도가 dedup(5s)을 타 커밋된 키를 Redis 엔트리 없이 재반환.
  → dedup 재사용 전 `EXISTS key:vk:{hash}` 확인, 없으면 신규 발급
  (EXISTS 자체 실패도 fail-safe 로 신규 발급).
- **S1 drain 의 gather 가 취소를 전파** — 요청 취소 시 커밋된 폐기의 DEL 태스크까지
  죽음(롤아웃/클라이언트 끊김). → `asyncio.wait` 로 교체(자식 생존,
  `_DEFERRED_TASKS` 강참조), 취소된 태스크는 surfaced 전파에서 제외.
- **S2 응답이 best-effort 쓰기까지 대기** — SCAN+DEL 무효화/실패 기록용 두 번째
  커넥션 대기가 응답을 잡아먹음. → surfaced 만 기한 없이 대기, 나머지는
  2s 상한(`_DEFERRED_DRAIN_TIMEOUT`) 후 백그라운드 계속.
- **S3 surfaced 실패가 drain 못 타면 무추적** — `_guarded` 가 raise 전
  warning 로그를 남기도록 변경.
- **N1** `POST /service-tokens/{id}/end-grace` 신설 — grace 창만 닫고 후속
  토큰은 살린다(DELETE 의 전체 사슬 폐기와 구분, docstring 명시).
- **N2** get_db_session 의 pending flush 를 commit 과 같은 try 안으로(응답 후
  flush 실패도 `commit_failed_after_response` 로 남김).
- **N3** `drain_pending_redis` 가 지연 태스크 기록도 함께 비움 — 비-요청
  세션(스케줄러/sync)의 session.info 적재 방지.

검증: admin-api **1000 passed, 50 skipped** — 회귀 +4(admin unpin 양방향,
sync caller 비덮기, dedup Redis 엔트리 부재 시 신규 발급 + 생존 시 재사용).

## 라운드 5 — NOT CONVERGED (S1 + N7)

라운드 4 수정의 잔여 경합 하나 발견 — r4 의 EXISTS 확인이 해결한 건
"SET 실패 후 재시도" 뿐이고, "SET 이 아직 in-flight" 인 동시 발급 창은 남아
있었다.

- **S1 in-flight SET 상태의 키를 동시 발급이 회전** — 요청 A 가 커밋하고 SET
  태스크가 아직 안 끝난 창에 요청 B 가 dedup 후보를 읽으면 `EXISTS` = 0 →
  `expire_and_create` 가 K_A 를 EXPIRED 로 → A 는 200 을 받았는데 곧 401
  (dead credential 재생산). → EXISTS miss 시 회전 대신 **repair-in-place**:
  FOR UPDATE 재확인(ACTIVE·미만료) 후 멱등 `SETEX key:vk` (잔여 TTL) +
  `SADD team:vk_hashes` 를 surfaced 지연 쓰기로 등록하고 같은 키 반환.
- **N1** `drain_pending_redis` 의 지연 태스크 비우기가 publish 없음/redis 없음
  조기 반환 뒤에 있어 비-요청 세션에서 적재 → 맨 앞으로 + info dict 가드.
- **N2** surfaced drain raise 가 커밋 완료 데이터의 publish 를 건너뜀 →
  CommittingRoute 두 지점 모두 `try/finally` 로 publish 보장.
- **N3** get_db_session 응답-후 drain 순서를 CommittingRoute 와 동일하게
  (deferred 먼저) + 중복 주석 정리.
- **N4** surfaced 를 best-effort 보다 먼저 기다림 — 실패 시 즉시 raise,
  best-effort 는 남은 시간 예산(`2s - elapsed`)만 대기.
- **N5** 취소된 surfaced 태스크(사실상 루프 종료)는 `error` 로그.
- **N6** end-grace: `get_by_id(for_update=True)` + 감사 `before.revoked_at`.
- **N7** 관리자 cross-team `team_source='sync'` 요청은 거부 — 다음 로그인에서
  그룹-유도 팀으로 되돌아가 부수효과가 이중 발화하므로, unpin 은 같은 팀에만
  허용(cross-team 은 'manual').

검증: admin-api **1001 passed, 50 skipped** — dedup 테스트를 repair-in-place
기대로 갱신 + 잠금 재확인에서 폐기된 키는 회전하는 대조군 추가.

## 라운드 6 — CONVERGED (N6, 전부 처리)

라운드 5 수정 전부 재실측 통과(reviewer 가 r5 변경의 시나리오·비동기 의미·
after_commit 발화 조건까지 각각 확인). M/S 없음, N 6건:

- **N1** `remaining` 이 0.x 초일 때 `int()` → 0 → `SETEX 0` 에러 → surfaced
  500. → `max(1, int(...))` 올림.
- **N2** CommittingRoute pre-commit drain 이 큐를 통째로 비움 — 현재 발행자는
  전부 after_commit 경유라 불변이 성립(latent). 커밋 전 직접 enqueue 하는
  신규 호출자 금지를 코드 주석으로 못 박음.
- **N3/N4** 스테일 주석 정정(EXISTS 실패=fail-safe 발급 → 보수 경로 기술,
  user 재사용 문구).
- **N5** budget_cache drain 주석의 gather → asyncio.wait 정정.
- **N6** `TransferUserRequest.team_source` 스키마 주석에 "sync 는 같은 팀
  요청에만 유효(cross-team 400)" 명시.

검증: admin-api **1001 passed, 50 skipped**.

## 사전존재/의도적 보류(비차단)

- reviewer 지적의 관련 경합(커밋 전 발급 충돌 → 재시도가 기존 키 회전)은
  pre-existing 으로 더 넓은 창 — per-user 락도 commit 시점에 풀려 SET 이
  늦게 끝나는 한 완전 봉쇄 불가. r5 repair-in-place 가 노출을 크게 줄임.
- `AsyncMock` 미대기 RuntimeWarning 다수 — 기존 테스트 픽스처 소음.
- `pytest.mark.unit` 미등록 마크 경고 — pytest ini 등록 필요(비차단).

## 최종 상태

6라운드 kiro/opus55 실측 리뷰로 수렴. admin-api 관리 쓰기 경로 전체
(키 발급·회수·dedup, 예산, 사용자/팀 이관, 서비스 토큰, 내부/감사/모니터링/
CLI 라우터, 레이트리밋) 커버. DB 마이그레이션 0048 (`users.team_source`) 포함
— 배포 시 migration 훅이 적용한다.
