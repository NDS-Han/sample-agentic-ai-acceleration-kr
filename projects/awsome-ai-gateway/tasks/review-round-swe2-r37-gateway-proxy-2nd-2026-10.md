# R37 — gateway-proxy 2차 리뷰 (횡단 관심사)

날짜: 2026-10 · 대상: R20/R22/R23 이후 변경 없음(`c75d1e92`) — 2차는 파일별 결함이 아닌
**횡단 점검**(인증 경로표·미들웨어 경계·env 발견성·테스트 전수).
베이스라인: `tests/unit` 663/663, `tests/`(integration 제외) **1422/1422**(28 skip = 실 Redis/PG 요구, 적절).

## 발견·수정

| # | 심각도 | 파일 | 관찰 | 조치 |
|---|---|---|---|---|
| 1 | **중간** | `services/auth_service.py` | `/v1/usage/team/{id}`가 `resolve_auth_strategy` 정확매칭에 없어 `/v1/` fallthrough로 **JWT 전용**. 핸들러는 "TEAM_LEADER(본인 팀) 또는 ADMIN"을 집행하지만 TEAM_LEADER는 VK만 소지 → **VK 호출이 인증 미들웨어에서 401로 죽어 권한 체크에 도달 불가**(계약상 사장 경로). 과거 실사용 이력은 `test_no_string_match_on_date_columns` 주석이 증명 | `path.startswith("/v1/usage/team/")` → `_DUAL_STRATEGY` 추가 + 회귀 테스트(`/v1/usage/me` 정합 포함) |
| 2 | 낮음 | `routers/openai_compat.py:911` | `asyncio.iscoroutinefunction` — Python 3.16 제거 예고 DeprecationWarning(테스트 전수에서 유일한 warning) | `inspect.iscoroutinefunction`으로 교체 + import |
| 3 | 낮음 | `.env.example` | config.py `Settings` 91필드 중 41개 미기재 — 대부분 튜닝 노브지만 `MAX_BODY_SIZE`/`WEB_SEARCH_ENABLED`/`CORS_ENABLED`/`BODY_LOG_S3_BUCKET` 같은 **운영자가 찾는 노브**의 발견 경로가 없음 | 파일 상단에 "전체 노브는 config.py가 단일 출처" 포인터 + 주요 노브 색인 추가 |

## 검증된 정상 (횡단)

- **미들웨어 순서**: `StateInjection(outermost)→OTel→MaxBody→ClientId→Auth→ClientAuthZ→Budget→Downgrade→RateLimit→HeaderInjector→router` — `add_middleware` insert(0) 역순 주석과 실측 일치. MaxBodySize가 인증 앞(비인증 대형 요청 조기 차단) 의도대로 배치.
- **MaxBodySizeMiddleware**: Content-Length 선행 차단 + chunked 누적 차단 + "응답 시작 후 거부"의 ASGI 프로토콜 위반 회피(두 번째 start 대신 종결 body)까지 엣지케이스 완비.
- **count_tokens**: `/v1/messages` prefix라 budget **체크**는 탐(차감 아님 — 예산 초과 시 counting도 429, 의도된 게이트로 판정); rate-limit 대상 아님(무료 호출). cross-account 정합(어드버서리 리뷰 #4 흔적), 모델/앱 스코프 AND 게이트, `_strip_region_prefix` 처리 정상.
- **`identify_client`**: `originator` 헤더 `codex_desktop`도 `startswith("codex")`로 정상 분류 — 데스크톱 Codex 설정(`config.toml` http_headers)과 정합 확인. 스푸핑 위협모델 문서화(R3-7)가 허용-리스트 권한상승 불가/예산버킷·계정 선택 가능 경계를 정확히 기술.
- **JWTAuthStrategy**: keyset Redis 캐시(60s)+로컬 사본 → 위조 kid가 DB를 치지 못하는 DoS 방어 + DB 장애 시 만료 사본으로 서명 검증 지속 — 설계 정합.
- **DualAuthStrategy**: `_is_jwt_token`으로 VK/JWT 판별 — 어느 크리덴셜이든 정확히 한 전략으로 위임.

## 판정 보류

- **`/v1/embeddings` 등 미구현 /v1/** — JWT 전용 fallthrough에서 401/404. 스펙에 없는 표면이라 의도된 미구현.
- **`/model/*`의 패스스루 와이어 형식**(개행 없는 JSON 연결) — ARCHITECTURE §9 표에 이미 "확인 필요"로 기록된 알려진 계약. 별도 작업 항목 유지.
- **client 스푸핑 → 예산버킷/계정 선택** — R3-7 문서화된 설계 한계. VK↔허용앱 바인딩은 스키마/발급 플로우 변경이 필요해 기능 갭(R36 목록 외 별개 설계 항목)으로 보류.

## 회귀 테스트

- `test_resolve_auth_strategy_usage_team` — `/v1/usage/team/{id}` → DUAL + `/v1/usage/me` 정합 유지.
