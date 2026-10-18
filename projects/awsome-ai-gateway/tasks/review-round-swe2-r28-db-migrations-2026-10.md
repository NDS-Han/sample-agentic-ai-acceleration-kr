# R28 — db/ 마이그레이션 + init SQL + 러너 리뷰

**라운드 방식**: SWE-2 직접 리뷰. 48개 버전 전수 스캔 + init SQL 8개 + env.py/run_migration.sh/Dockerfile 정독 + 실 PG16(r18-poison-pg) 실측.
**범위**: `db/` 전체. R24가 마이그레이션 Job *배선*만 봤던 것에 이어 DDL 본체.

## 검증 결과

| 항목 | 결과 |
|---|---|
| db/tests | **26 passed** (기존 17 + 신규 9) |
| `sh -n` / shellcheck | clean (SC2086 의도분리는 disable 주석) |
| `\gexec`+`format()` 인용 | 실 PG16 실측 — 롤명 `"`+공백, 비번 `'`+`"`+`$` 통과 |
| `server_settings` | asyncpg 시그니처 + `SHOW lock_timeout`=1min 실측 |
| 전 버전 upgrade/downgrade 존재 | 48/48 |

## 처리한 발견

- **alembic 세션에 lock/statement/idle 타임아웃 부재 (S)** — `transaction_per_migration=True`라 각 마이그레이션이 자체 tx. `usage_logs` 등 hot 테이블의 DDL이 긴 쿼리 뒤에 큐잉하면 이후 모든 게이트웨이 INSERT가 그 뒤에 줄서서 쓰기 정지. `server_settings`(asyncpg `connect_args`)로 `lock_timeout=60s`/`statement_timeout=15min`/`idle_in_transaction_session_timeout=10min` 기본값 + `DB_MIGRATION_*` env로 튜너블. 락 못 잡으면 빠른 실패 → Job 재시도로 재개(전 버전 멱등).
- **`run_migration.sh` 비밀값 미인용 삽입 (S)** — `'${APP_PASSWORD}'`가 `'` 포함 시 DO 블록 파괴/주입. 대안 각각의 함정(-v→argv 노출, \set→공백 분리, `:'var'`→DO$$ 내부 치환 불가)을 피하는 방식으로 재작성: 셸에서 `'→''` / `"→""` 두배화 이스케이프 → stdin heredoc 리터럴 SQL → `\gexec`+`format()` 분기. 실 PG16으로 적대적 입력 검증.
- **`MASTER_URL_NO_PASS`가 URL user를 DB_MASTER_USER로 덮어씀 (N)** — 두 값이 어긋나면 원인 불명 인증 실패. sed 백레퍼런스로 URL user 보존.
- **`psql -X` 부재 (N)** — 운영자 수동 재실행 시 `~/.psqlrc` 임의 SQL이 master로 실행될 경로.
- **psql 단계 lock_timeout 부재 (N)** — `PGOPTIONS`로 init/GRANT에도 동일 기본값.
- GRANT 루프 40개 psql 세션 → 스키마당 1개(총 8)로 통합.

## 확인됨(수정 불요) — 이미 경화된 표면

- `env.py` 자격증명 리댁션 — `rfind("@")` 기반, 과거 실측 유출 사례까지 주석화
- init SQL 글롭 루프 — 08 누락 실사고 교훈 반영, 드리프트 가드 테스트 존재
- `04_create_users` NOLOGIN + 기존 롤 비밀번호 무효화 ALTER 분기
- `08_create_chat_reader` 랜덤 비번 + 컬럼 단위 whitelist + 명시 REVOKE + 10s statement_timeout
- enum ADD VALUE는 migration-per-commit 분리(0008/0009)로 PG12+ `unsafe use` 회피 — PG16 배포와 정합
- CONCURRENTLY 미사용 — 0026/0034 docstring에 트랜잭션 제약과 근거 명시
- `NOT NULL DEFAULT 상수` 추가들 — PG11+ 메타데이터 전용이라 안전(배포 PG16)
- `0003` DROP…CASCADE — FK 참조 없음 + init이 pricings를 시드 안 해 신규 DB 유실 없음
- `0024/0034/0040/0045` 유니크 인덱스 — 생성 전 dedupe·사전검사·명확한 예외
- `0045`는 `SET LOCAL lock_timeout='5s'` 이미 사용 — 세션 기본값과 충돌 없음
- init의 장문단가 UPDATE — `IS NULL` 값가드로 재실행 안전, 신규 DB는 0042가 채움

## 잔여/후속

- `alembic.ini`의 `sqlalchemy.url` dev placeholder(`gateway:gateway`) — env 미설정 로컬 실행용, compose 기본값과 일치
- init SQL이 alembic_version을 스탬프하지 않음 — 전 버전 멱등이라 정합이지만 새 마이그레이션 추가 시 비멱등 DDL 금지 규칙이 계속 필요(테스트로 가드됨)
- 운영 DB 단위의 `lock_timeout` 기본값 60s가 큰 클러스터에서 빡빡하면 `DB_MIGRATION_LOCK_TIMEOUT` env로 조정
