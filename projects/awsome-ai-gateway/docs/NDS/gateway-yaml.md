# gateway.yaml 설정 설명서

`deployment/gateway.yaml` 은 배포의 **유일한 설정 원본**입니다. 이 파일을 고치고
`./deploy render`(compose) 또는 해당 백엔드 apply 를 실행하는 것이 모든 변경의
표준 경로입니다.

## 전체 구조

```yaml
version: 1                    # 스키마 버전 — 1 고정
env: my-gw                    # 환경 이름 (소문자·숫자·하이픈)

aws:
  region: ap-northeast-2      # Bedrock·배포 리전
  account_id: "123456789012"  # ecs/eks 에서 필요할 수 있음

deploy:
  target: compose             # compose | ecs | eks
  size_tier: t0               # t0 | t1 | t2 | t3
  sizing: {}                  # 티어 기본값 세부 override (선택)

network:
  mode: public                # public | private
  allowed_cidrs: ["1.2.3.0/24"]

domain:
  mode: none                  # none | cloudfront-temp | route53-acm
  name: example.com           # route53-acm 일 때 필수
  zone_id: Z123...            # Route53 hosted zone (eks)

features:
  notifications:
    provider: mock            # mock | smtp | ses
    smtp_host: ""
    smtp_port: 587
    smtp_from: ""
    ses_from: ""
  bi_insight: false
  pricing_lambda: false
  web_search: false
  body_logging: false
  observability: false        # OTel/Prometheus/Grafana (compose 전용 스위치)

images:
  registry: ""                # ECR 레지스트리 — ecs/eks 필수
  tag: ""                     # 이미지 태그 — ecs/eks 필수 (명시적 핀)

clients:
  models_profile: global      # global (권장) | regional

oidc:                          # 비워두면 OIDC 비활성(dev-login)
  issuer_url: ""
  audience: ""
  client_id: ""
  authorize_url: ""
  token_url: ""
  provider_name: oidc:cognito
  required_group: ""
```

## 항목별 상세

### `deploy.target` / `deploy.size_tier`

허용 조합만 됩니다:

| target | 가능한 tier |
|---|---|
| compose | t0 만 (단일 호스트) |
| ecs | t1, t2 |
| eks | t2, t3 |

### `domain.mode`

| 값 | 의미 | TLS |
|---|---|---|
| `none` | 도메인 없음 — 포트별 접속(8000/8080/3000) | HTTP 만 |
| `cloudfront-temp` | CloudFront 기본 도메인으로 임시 https | 있음 (EKS 전용) |
| `route53-acm` | 실제 도메인 + 인증서 | 있음 |

도메인은 **나중에 바꿀 수 있습니다** — 다만 이미 설치된 클라이언트의 게이트웨이
주소가 바뀌므로 재안내가 필요합니다.

`route53-acm` + `name: example.com` 이면 `gateway.` `admin-api.` `admin.` 세
서브도메인이 자동으로 쓰입니다.

### `features`

| 키 | 켜면 생기는 것 | 주의 |
|---|---|---|
| `notifications.provider` | mock=발송안함, ses=SES 발송, smtp=외부 SMTP | ses는 `ses_from` 필수, smtp는 `smtp_host` 필수 |
| `bi_insight` | BI 어시스턴트 (AgentCore Runtime) | compose 미지원. 별도 이미지 배포 필요 |
| `pricing_lambda` | LiteLLM 단가 조회 Lambda | 단가 데이터는 DB 시드가 따로 필요 |
| `web_search` | AgentCore 기반 웹검색 | **us-east-1 전용 인프라**, compose는 수동 프로비저닝 |
| `body_logging` | 요청 본문 S3 로깅 | 인프라 + 관리자 런타임 토글 이중 게이트 |
| `observability` | OTel/Prometheus/Loki/Tempo/Grafana | compose 경로에서 기본 off |

### `oidc`

admin-api(JWT 검증)와 admin-ui(브라우저 SSO)가 공유하는 설정입니다.

- 비워두면: OIDC 비활성 → admin-ui는 dev-login으로만 진입 (**운영 전환 시 반드시 설정**)
- 채우면: `DEV_LOGIN_ENABLED=false` 자동 적용
- `authorize_url`/`token_url` 은 issuer가 **아닙니다** — Cognito hosted-ui 도메인
  (`https://<domain>.auth.<region>.amazoncognito.com/oauth2/...`)입니다

### `clients.models_profile`

- `global` (기본·권장): `global.anthropic.*` inference profile — 어느 리전에서든 동작
- `regional`: 리전별 프로필 — 특정 리전 정책이 필요한 경우만

## 변경 후 절차

```bash
vi deployment/gateway.yaml
./deploy validate          # 스키마 검증 (실수 조기 발견)
./deploy render            # compose 산출물 재생성 (시크릿 보존)
docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d
./deploy doctor            # 결과 확인
```

## 수동으로 바꾸면 안 되는 것

`deployment/gen/<env>/` 안의 파일은 전부 생성물입니다 — 직접 고치면 다음
`render` 때 덮어씌워지고 `doctor` 가 drift로 보고합니다. 바꾸고 싶은 값이
gateway.yaml에 없으면 스키마에 노브를 추가하세요.

예외: `.env` 에 `AWS_ACCESS_KEY_ID` 같이 **스키마에 없는 키를 추가하는 것**은
허용됩니다 — render는 있는 키를 보존하고 없는 키만 채웁니다.
