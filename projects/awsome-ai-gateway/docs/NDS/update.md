# 업데이트 · 롤백

모든 변경은 `gateway.yaml` 하나가 원본이고 `./deploy`가 나머지를 합니다.
일상 경로는 **`./deploy configure`(항목별 확인 → 배포까지)**, 파일 직접 편집 경로는
**`./deploy apply`(render 내장)** 입니다 — 환경별 스크립트를 찾아 돌릴 필요가 없습니다.

## 기존 배포를 gateway.yaml 로 온보딩 (capture)

이미 배포된 환경(스크립트/수작업)을 선언적 관리로 가져옵니다:

```bash
# 1. 현재 배포 상태를 읽어 gateway.yaml 후보 생성
./deploy doctor --capture --gen-dir deployment/gen/<env>
#    → deployment/gateway.captured-<env>.yaml 생성 (기존 yaml은 덮지 않음)

# ecs 는 terraform state 를 읽으므로 AWS 자격이 필요합니다
./deploy doctor --capture --target ecs --region ap-northeast-2

# 2. 후보 파일 검토 — capture notes 가 못 읽은 값을 알려줍니다
vi deployment/gateway.captured-<env>.yaml

# 3. 검증 후 gateway.yaml 로 채택
./deploy validate --config deployment/gateway.captured-<env>.yaml
mv deployment/gateway.captured-<env>.yaml deployment/gateway.yaml
```

기존 `gateway.yaml` 이 있는 상태에서 capture 하면 **차이 테이블**이 나옵니다
(yaml 값 ←→ 배포된 실제 값). `--interactive` 를 붙이면 항목별로 선택합니다:

- **absorb** — 실제 값을 yaml 에 채택 (수동 변경을 정식 설정으로 흡수)
- **keep** — yaml 유지 (다음 `apply` 가 실제 상태를 yaml 대로 되돌림)
- **skip** — 지금은 건너뜀

> 캡처가 못 읽는 값: `.env` 의 시크릿, compose 의 `network.mode`, ecs 의
> `zone_id`/`allowed_cidrs`/`tfstate_*` — 파일 끝의 `# capture notes` 를
> 확인하고 직접 채우세요.

**eks 캡처** — helm release 의 effective values + kubectl 라이브 상태를 읽습니다
(kubeconfig 에 클러스터 접근이 필요):

```bash
./deploy doctor --capture --target eks \
  [--namespace llm-gateway] [--release llm-gateway] [--context <ctx>] \
  [--env-dir deployment/terraform/environments/llm-gateway-dev]
```

읽는 것: 리전, 도메인/CIDR(ingress 어노테이션), notification provider,
feature 플래그(WEB_SEARCH/FIREHOSE/otel), OIDC 계약, 서비스별 이미지 태그.
라이브 ingress/deployment 와 values 가 다르면(kubectl 패치·set image) notes 로
드리프트를 알립니다. 서비스별로 태그가 다른 것(단일 `images.tag`로 표현 불가),
ESO/Fargate/RDS Proxy 등 eks 고유 구성도 notes 에 기록됩니다 — 캡처본은
**온보딩용 후보**이지 eks 재배포 가능한 완전한 선언이 아닙니다.

## 일반 업데이트 (기능 on/off, 설정 변경)

가장 쉬운 경로 — 현재 설정을 기본값으로 항목을 다시 물어보는 마법사:

```bash
./deploy configure     # 항목별 확인(Enter=유지) → 저장 → "지금 배포?" y
                       # → render → plan 미리보기 → 확인 → apply 까지 자동 연결
./deploy doctor        # ✓ 확인
```

파일을 직접 고치는 경로도 그대로 지원합니다:

```bash
vi deployment/gateway.yaml        # 변경
./deploy validate                 # 검증
./deploy apply --plan             # 변경 미리보기 (아무것도 안 바뀜)
./deploy apply                    # 확인 → 실제 적용
./deploy doctor                   # ✓ 확인
```

`doctor` 가 `.env` drift, 서비스 이상, 마이그레이션 head 불일치를 보고하면
해당 메시지대로 조치하세요.

## 코드 업데이트 (새 버전 배포)

```bash
cd awsome-ai-gateway
git fetch && git checkout <새 브랜치/태그>
./deploy render
docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d --build
./deploy doctor
```

마이그레이션은 `migration` 서비스가 기동 시 자동으로 `alembic upgrade head`
까지 올립니다 — 앱보다 먼저 완료되도록 depends_on으로 순서가 보장됩니다.

## 롤백

plane 별로 다릅니다 — "태그만 되돌리기"는 롤백이 아닙니다:

| 무엇이 바뀌었나 | 되돌리는 법 |
|---|---|
| 앱 코드/이미지 | 이전 커밋/태그로 `render` + `up -d` |
| 설정(gateway.yaml) | 이전 값으로 되돌려 `render` + 재기동 |
| **DB 마이그레이션** | 다운그레이드 없음 — forward-fix (새 마이그레이션으로 수복) |
| `.env` 삭제/유실 | **백업에서 복원** — VIRTUAL_KEY_ENCRYPTION_KEY를 잃으면 발급된 Virtual Key 전부 재발급 필요 |

## 도메인을 나중에 얻었을 때

```bash
# gateway.yaml
domain:
  mode: route53-acm       # compose는 caddy 가 자동 TLS
  name: example.com

./deploy render && docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d
```

DNS에 `gateway.`/`admin-api.`/`admin.` `example.com` A레코드(서버 IP)가 먼저
있어야 인증서가 발급됩니다. 기존 설치된 클라이언트는 게이트웨이 주소가 바뀌므로
새 주소를 다시 안내해야 합니다.

**ecs** — `domain.zone_id`(Route53 hosted zone)도 함께 채운 뒤 `./deploy apply`.
ACM 인증서는 모듈이 만들고 DNS 검증 레코드도 자동 생성됩니다. hosted zone 이
다른 계정에 있으면 `terraform output cert_validation_records` 로 레코드를
확인해 수동 등록 후 재적용하세요. 도메인 도입 시 NEXTAUTH_URL 도 새 도메인으로
자동 갱신됩니다 — 세션 재로그인이 필요합니다.

## public → private 전환

단일 플래그 전환이 아니라 **순서 있는 작업**입니다:

1. 사용자가 게이트웨이에 도달할 경로를 먼저 확보 (VPN/DX/사내망)
2. `network.allowed_cidrs` 를 사내 대역으로 좁히고 `render` + 재기동
3. Caddy 가 허용 대역 외 접속을 403으로 차단 (SG로도 이중 차단 권장)
4. `network.mode: private` 로 표시 후 인스턴스를 프라이빗 서브넷으로 이전 — 이 단계는 서버 재배치라 계획된 다운타임 필요

**ecs** — `network.mode: private` 은 ALB 를 internal 로 바꾸고 프라이빗
서브넷을 씁니다. 전환 전에 ① VPN/DX 등 사내망→VPC 경로, ② 도메인 사용 시
프라이빗 hosted zone 또는 사내 DNS 해석이 필요합니다. `render` → `apply` 가
ALB 를 교체하므로 짧은 다운타임이 생깁니다 — 유지보수 창에서 진행하세요.
private 에서도 NAT 는 유지됩니다(ECR pull·Bedrock 호출 경로).
