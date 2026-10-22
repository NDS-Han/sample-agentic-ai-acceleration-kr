# R35 — 루트 Compose 심층 리뷰 (docker-compose.yml + idp overlay + renderer 전파)

날짜: 2026-10 · 대상: `docker-compose.yml`(~360줄), `docker-compose.idp.yml`(53줄),
`docs/NDS/deploy/render/{compose,common}.py` 전파 경로.
베이스라인: `docs/NDS` deploy 테스트 **213/213 passed** (신규 회귀 테스트 3건 포함).

## 전제

루트 `docker-compose.yml`은 dev 전용이 **아니다** — `render/compose.py`가 이 파일을
베이스 템플릿으로 읽어 `deployment/gen/<env>/docker-compose.yml`(t0)을 산출한다.
베이스의 결함은 모든 t0 배포로 전파될 수 있으므로, 본 라운드는 "무엇이 전파되는가"를
축으로 검토했다. CI 와 `admin-chat-agent`는 사용자 지시로 범위 제외.

## 전파 경로 (확인 사실)

- renderer 가 제거: `mock-vllm` 항상, observability 5종은 `features.observability=false`일 때.
- renderer 가 벗기는 것: `APP_SERVICES`(앱 7종) + `postgres`/`redis`의 `ports:` — **observability
  서비스의 ports 는 그대로 유지**(이번 발견 ①의 근본 원인).
- renderer 가 보정: `restart=unless-stopped` setdefault, gateway `stop_grace_period=300s`,
  이미지 핀(`images.tag` 시 build→image), Caddy 단일 인입 + IP allowlist.
- `.env`는 `GENERATED_KEYS` 중 **없는 키만** 생성·보존(재렌더 안전), 파일 0600.

## 발견·수정

| # | 심각도 | 파일 | 관찰 | 조치 |
|---|---|---|---|---|
| 1 | **높음** | `docker-compose.yml` observability 5종 | `observability=true` 렌더 시 Grafana :3001·Prometheus :9090·Loki :3100·Tempo :3200·OTLP :4317/4318·collector :8889가 **0.0.0.0 호스트 바인드로 전파** — Caddy allowlist 우회 경로. OTLP는 인증 없는 쓰기 엔드포인트(텔레메트리 주입), SG를 여는 순간 Grafana가 브루트포스 표적 | 전부 `127.0.0.1:` 루프백 바인드 + renderer note에 SSH 터널(`ssh -L 3001:localhost:3001`)·비번 위치 안내 추가 |
| 2 | **높음** | same + `render/common.py` | `GF_SECURITY_ADMIN_PASSWORD:-admin` 리터럴 폴백 + `GENERATED_KEYS`에 키 없음 → t0 `.env`에도 미생성, admin/admin 고정 | compose를 `:?`(unset/empty 시 기동 거부)로 fail-closed, `GENERATED_KEYS`에 `gen_password` 등록, `.env.example`에 dev/deploy 경계 주석 |
| 3 | **중간** | `docker-compose.yml` gateway-proxy | `~/.aws:/home/appuser/.aws:ro` dev 마운트가 t0에 전파 — 호스트 `~/.aws/credentials` 존재 시 SDK 체인상 static creds가 **instance profile(IMDS)보다 우선**해 `llm-gateway-host` 역할 의도를 조용히 무시 | renderer가 gateway-proxy의 `~/.aws:` 볼륨을 제거(남으면 volumes 키 자체 삭제) + 회귀 테스트 |
| 4 | 중간 | same postgres/redis | `5432`/`6379` 0.0.0.0 바인드 — 커밋된 dev 비번·무인증 redis가 dev 랩톱 LAN에 노출 | 루프백 바인드(t0는 어차피 ports 제거 — dev 전용 개선) |
| 5 | 낮음 | same 9종 + `idp.yml` | postgres/redis/gateway-proxy/admin-api/mock/observability에 `restart:` 없음(dev 크래시 영속 안 됨 — t0는 renderer setdefault로 보장됐음), keycloak `8088` 0.0.0.0 + admin/admin | `restart: unless-stopped` 통일(`migration`은 `no` 유지 — run-once), keycloak 루프백 |

## 검증된 경화 (변경 없음)

- **renderer**: `.env` 재렌더 시 기존 시크릿 보존(`VIRTUAL_KEY_ENCRYPTION_KEY` 변경=발급 VK 전부 무효화를 회피), `os.open(0600)`+`fchmod` 이중 권한, `migration`에 `DB_MASTER_URL` 부여(init SQL 재적용), Caddy `depends_on: service_healthy` 게이트, `stop_grace_period=300s`(uvicorn graceful+drain 커버).
- **depends_on 게이팅**: 앱 4종이 `postgres healthy` + `redis healthy` + `migration completed_successfully` — 부트 순서 정확.
- **idp overlay**: `start-dev`+realm import+`KC_HOSTNAME` 고정(backchannel dynamic) — dev PoC 용도로 적절, renderer 미소비 확인.

## 판정 보류

- **앱 포트 8000/8080/3000의 0.0.0.0 유지** — dev 직접 접근용(원격 dev box 워크플로) + 전부 인증 게이트 뒤 + t0에서 renderer가 제거. 의도된 dev 표면으로 판정.
- **서드파티 이미지 태그 mutable** (`pgvector/pgvector:pg16`, `redis:7-alpine`, `grafana/*:latest`, `caddy:2-alpine`) — 앱 이미지는 `images.tag`로 핀 가능, 인프라 이미지는 배포별 digest 핀 정책이 없어 보류.
- **`DEV_LOGIN_ENABLED:-true` 베이스 기본값** — OIDC 설정 시 renderer가 `false`로 덮음 확인, dev 편의로 보류.
- **Keycloak admin/admin** — dev 전용 overlay, 루프백 바인드로 축소 후 보류.

## 회귀 테스트 (신규 3건)

- `test_gateway_aws_mount_stripped` — 생성 compose에 `.aws` 볼륨 부재.
- `test_observability_ports_loopback_only` — observability=true 시 caddy 제외 전 포트 `127.0.0.1:` 접두.
- `test_grafana_admin_password_generated` — `.env`의 `GF_SECURITY_ADMIN_PASSWORD`가 비어있지 않고 `admin`이 아님.

## 잔여 과제

- 서드파티 이미지 digest/버전 핀 정책 (gateway.yaml 스키마 확장 여부) — 다음 배포 작업 시 결정.
- dev 경로의 `~/.aws` 마운트 자체는 유지(STS 로그인 dev 시나리오) — README에 "dev 전용, 배포 시 renderer 제거" 명시돼 있으나 신규 서비스 추가 시 같은 패턴 재발 가능. renderer 테스트가 가드 역할.
