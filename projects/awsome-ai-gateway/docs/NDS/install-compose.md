# t0 설치 — 단일 호스트 Docker Compose

**대상**: 평가·POC·소규모(~50명). 하나의 VM에 전체 게이트웨이를 올립니다.

완성되면: PostgreSQL + Redis + 마이그레이션 + 6개 앱 서비스 + Caddy(HTTP/HTTPS 인입점)

> ⚠️ **알고 시작하세요** — t0 는 단일 노드입니다. 서버가 죽으면 서비스가 멈추고,
> 복구는 백업에서 합니다. 100명 이상·운영 중요도가 있으면 `ecs`(t1) 경로를 선택하세요.

---

## 0. 준비물

| 항목 | 요구 |
|---|---|
| 머신 | Linux VM 1대 (권장 4 vCPU / 16GB, 최소 2 vCPU / 8GB). EC2 t3.xlarge 기준 |
| 소프트웨어 | Docker Engine 24+ 와 Compose 플러그인 (`docker compose version` 으로 확인), git |
| AWS | Bedrock 모델 접근이 켜진 계정 + 자격증명(EC2 instance profile 또는 access key) |
| Bedrock 모델 | Claude 모델의 `global.*` inference profile 사용 가능 리전 (ap-northeast-2 등) |
| 네트워크 | 사용자 PC → 이 서버의 80/443(도메인 있음) 또는 8000/8080/3000(도메인 없음) 도달 가능 |
| 선택 | HTTPS용 도메인 — 없어도 시작 가능, 나중에 추가 가능 |

### AWS 자격증명 두 가지 방법

**EC2 instance profile (권장)** — 인스턴스에 `bedrock:InvokeModel` 권한 IAM role 부여. 추가 작업 없음.

**Access key** — `.env` 에 `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` 를 추가 (아래 4단계).

---

## 1. 저장소 받기

```bash
git clone <이 저장소> awsome-ai-gateway
cd awsome-ai-gateway/projects/awsome-ai-gateway
```

## 2. 배포 설정 만들기 — `./deploy init`

```bash
./deploy init
```

질문에 답하면 `deployment/gateway.yaml` 이 생성됩니다:

```
환경 이름: my-gw
AWS 리전 [ap-northeast-2]: ap-northeast-2
배포 대상: compose
규모 티어: t0
HTTPS 도메인: none          ← 도메인이 없으면 none. 있으면 route53-acm
알림 provider: mock         ← 이메일 발송이 필요하면 ses/smtp
추가 기능: (스페이스로 선택, 기본 전부 off)
OIDC 로그인 설정: y → Cognito issuer/client 정보 입력
```

생성된 파일 예시:

```yaml
version: 1
env: my-gw
aws: { region: ap-northeast-2 }
deploy: { target: compose, size_tier: t0 }
network:
  mode: public
  allowed_cidrs: ["203.0.113.0/24"]   # ← 회사/VPN 대역을 넣으세요
domain: { mode: none }
features:
  notifications: { provider: mock }
clients:
  models_profile: global
```

> `allowed_cidrs` 를 비우면 **인터넷 전체에 게이트웨이가 열립니다** — 반드시
> 사용자가 접속하는 공인 IP 대역을 넣으세요.

## 3. 산출물 생성 — `./deploy render`

```bash
./deploy render
```

`deployment/gen/<env>/` 에 세 파일이 생깁니다:

| 파일 | 내용 |
|---|---|
| `docker-compose.yml` | 배포용 서비스 집합 (로컬개발용 mock·관측스택 제거됨) |
| `.env` | DB 비밀번호·VK 암호화키·세션 시크릿 — **자동 생성, 권한 0600** |
| `Caddyfile` | 포트/도메인 라우팅 + IP 허용목록 |

> ⚠️ **`deployment/gen/<env>/.env` 를 잃으면 발급된 Virtual Key가 전부 무효화됩니다.**
> `VIRTUAL_KEY_ENCRYPTION_KEY` 값을 비밀번호 관리자나 Secrets Manager에 별도 보관하세요.

시크릿은 다시 `render` 해도 바뀌지 않습니다 (없는 키만 채움).

## 4. 기동

```bash
docker compose --env-file deployment/gen/<env>/.env \
  -f deployment/gen/<env>/docker-compose.yml up -d --build
```

> ⚠️ **`--env-file` 을 빼면 안 됩니다.** 없으면 compose가 `${POSTGRES_PASSWORD}`
> 등을 repo 루트의 `.env`나 기본값으로 interpolate 해서 **앱과 DB의 비밀번호가
> 어긋납니다.** `deploy apply` 명령은 이 플래그를 자동으로 붙입니다:
> `./deploy apply --build`

첫 실행은 이미지 빌드에 10~20분 걸립니다. 이후 `up -d` 만으로 재기동됩니다.

AWS 자격증명을 access key로 쓰는 경우, `deployment/gen/<env>/.env` 에 추가:

```bash
AWS_ACCESS_KEY_ID=AKIA...
AWS_SECRET_ACCESS_KEY=...
```

## 5. 확인 — `./deploy doctor`

```bash
./deploy doctor
```

```
✓ [OK  ] artifacts: render 산출물 존재
✓ [OK  ] secrets: 필수 시크릿 존재
✓ [OK  ] services: postgres healthy
✓ [OK  ] services: migration 완료 (exited)
✓ [OK  ] services: gateway-proxy healthy
✓ [OK  ] services: admin-api healthy
✓ [OK  ] migration: alembic head = 0040
✓ [OK  ] drift: .env 가 gateway.yaml 과 일치
```

전부 ✓ 이면 설치 완료입니다. ✗ 가 있으면 해당 항목 메시지대로 조치하세요.

## 6. 접속 주소

**도메인 없이 설치한 경우** (`domain.mode: none`):

| 서비스 | 주소 |
|---|---|
| 게이트웨이 (클라이언트가 쓸 주소) | `http://<서버IP>:8000` |
| Admin API | `http://<서버IP>:8080` |
| Admin UI | `http://<서버IP>:3000` |

**도메인으로 설치한 경우**: `gateway.<도메인>`, `admin-api.<도메인>`, `admin.<도메인>` (자동 HTTPS)

> Cowork(Claude Desktop)는 `https://` 주소만 받습니다 — Cowork 사용자가 있으면
> 도메인을 준비하고 `domain.mode` 를 변경해 재배포하세요 ([update.md](update.md)).

## 7. 첫 관리자 로그인

OIDC를 설정했다면 Admin UI에서 SSO 로그인. 설정하지 않았다면 dev-login 버튼으로
들어간 뒤 **반드시 OIDC를 설정하고 `DEV_LOGIN_ENABLED=false` 로 바꾸세요**
(`gateway.yaml`에 `oidc:` 블록 추가 → `render` → 재기동).

## 8. 백업 설정 (중요 — 단일 노드라 백업이 전부입니다)

```bash
# DB 백업 (cron 등록 예시 — 매일 02:00, 7일 보관)
0 2 * * * docker compose -f ~/awsome-ai-gateway/projects/awsome-ai-gateway/deployment/gen/<env>/docker-compose.yml \
  exec -T postgres pg_dump -U gateway gateway | gzip > ~/backups/gateway-$(date +\%F).sql.gz
```

함께 보관할 것: `deployment/gen/<env>/.env` (특히 `VIRTUAL_KEY_ENCRYPTION_KEY`).

---

## 문제가 생기면

| 증상 | 확인 |
|---|---|
| 컨테이너가 안 뜸 | `docker compose -f gen/<env>/docker-compose.yml logs <서비스>` |
| 마이그레이션 실패 | postgres 이미지가 `pgvector/pgvector:pg16` 인지 확인 (stock postgres는 실패) |
| Bedrock 호출 4xx/5xx | 서버의 AWS 자격증명·리전·모델 접근 권한. `docker compose logs gateway-proxy` |
| 로그인이 안 됨 | OIDC 4개 값(issuer/client/authorize/token) 확인 — hosted-ui URL은 issuer가 아님 |
| 클라이언트 403 | 사용자 PC 공인 IP가 `allowed_cidrs` 안에 있는지 |
