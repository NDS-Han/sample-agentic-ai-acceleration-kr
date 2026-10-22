# R38 — admin-api 2차 리뷰 (라우터 표면·권한 경계·테스트 위생)

날짜: 2026-10 · 대상: R21(쓰기)/R25(읽기) 이후의 횡단 재점검 — 전 라우터 권한 데코레이터 표면 대조,
TEAM_LEADER 스코프 격리, 공개 엔드포인트 DoS 표면.
베이스라인: `pytest` **1012/1012**(50 skip) — 경고 102→13건으로 정리.

## 발견·수정

| # | 심각도 | 파일 | 관찰 | 조치 |
|---|---|---|---|---|
| 1 | 낮음 | `routers/monitoring.py:115` | `Query(regex=...)` — FastAPI deprecation(`pattern` 이관) 경고 | `pattern=` 로 교체(검증 정규식 동일) |
| 2 | 낮음 | `pyproject.toml` | 테스트가 `@pytest.mark.unit`/`integration`을 쓰는데 미등록 — PytestUnknownMarkWarning 80+건이 실행 출력을 오염시켜 진짜 경고를 묻음 | markers 등록 |

## 검증된 권한 경계 (재확인)

- **전 라우터 표면**: 쓰기 경로는 전부 `require_admin` 또는 `require_admin_or_team_leader` + 서비스층 스코프 검증. 미인증 경로는 `exchange`(자체가 인증), `webhooks/git`(HMAC 서명 dep), `cli/*`(STS presigned 검증), `/internal/*`(공유 시크릿 + prod 게이트 이중)만 — 의도된 표면.
- **TEAM_LEADER 격리(BR-BUD-03)**: `set_user_budget` — 본인 팀 멤버만 + 자기 자신 금지(self-allocation) + 다른 리더/관리자 대상 금지(상호배정 우회 차단). `allocate_team_budget`/`get_user_app_budgets`/`summary`/`/{scope}/{id}` 전부 동일 규칙 — `summary`는 쿼리 파라미터 우회 시도도 `_leader_allowed_team_ids`(소속+리드 팀)로 재필터.
- **`/exchange`**: per-IP 고정창 10RPM + XFF 오른쪽 홉(프록시 신뢰 계약) + 이중 봉투 회귀 테스트 못박음 — 공개 무게 쓰기 경로의 DoS 표면 최소화.
- **`cli/download/{os}/{arch}`**: allowlist 조합만 서빙 — 경로 탐색 원천 불가.
- **`my.py`**: `get_current_user`(역할 무관 자기 데이터만) — 의도된 인증 수준.
- **`internal.py`**: `X-Internal-Token` + prod 게이트 이중, 미설정 시 fail-closed.

## 판정 보류

- `cache_invalidation.py:68` 의 `coroutine never awaited` RuntimeWarning — **테스트 아티팩트**(AsyncMock `scan_iter` 미해결). 실 코드는 async-iter를 정상 소비. 테스트 쪽 mock 형태 문제라 코드 결함 아님.
- `scheduler/main.py:205`의 `NotImplementedError` 캐치 — Windows 미지원 신호 처리 방어, 의도.
- admin-api config와 `.env.example` 커버리지는 별도 확인 예정(R41 배포/문서 일치 라운드로 이관).

## 관측 — 다음 라운드로 이월

- `ProductivityEvent`/git webhook 파이프라인은 `usage.productivity_events`/`git_events` 스키마가 있으나 대시보드 표면이 아직 `/admin/analytics/productivity` 읽기 하나 — R39 UI 리뷰에서 노출 상태 점검.
