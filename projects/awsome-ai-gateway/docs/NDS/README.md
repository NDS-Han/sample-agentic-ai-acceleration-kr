# NDS 배포 가이드 — gateway.yaml 기반 통합 배포

LLM Gateway(Claude Code · Cowork · Codex 지원)를 **어느 AWS 계정·리전에서든**
같은 방법으로 배포하는 표준 절차입니다. 기존 `docs/us-llm-gateway/`는 특정
사이트(US, us-west-2)에 고정된 운영 문서이고, 이 디렉토리는 범용입니다.

핵심 아이디어: **설정은 `deployment/gateway.yaml` 한 파일**이 원본이고,
`./deploy` 하나가 그 파일을 읽어 배포 대상에 맞는 모든 것을 만듭니다.
환경별 스크립트를 찾아 돌리는 방식이 아닙니다.

## 어떤 경로로 배포할까

| 상황 | 배포 대상 | 용량 선언 | 문서 | 뭐가 생기나 |
|---|---|---|---|---|
| 평가·POC·테스트 | `compose` | **t0 고정** (테스트·평가용 최저 티어만) | [install-compose.md](install-compose.md) | EC2 1대에 Docker Compose로 전체 스택 |
| 소규모~표준 운영 | `ecs` (t1/t2) | `capacity_tier` 밴드 → task 수 | [install-ecs.md](install-ecs.md) | ECS Fargate + Aurora + ElastiCache — **인프라까지 전부 생성** |
| EKS 신규 구축·대규모 | `eks` (t2/t3) | `capacity_tier` 밴드 → pod 수 | [install-eks.md](install-eks.md) | `bootstrap`이 인프라+앱 전체 생성 / `apply`로 업데이트 |

> **용량은 사용자 수로 선언합니다** — `deploy.capacity_tier: "500" | "1000" |
> "3000" | "6000" | "6000+"` 한 줄이 eks pod min/max 와 ecs task 수를
> 밴드로 전개합니다 (세부: [gateway-yaml.md](gateway-yaml.md#deploycapacity_tier--사용자-수-밴드-ecseks)).
> 개별 키는 `deploy.sizing` 으로 덮습니다. compose 는 복제본이 없어
> 용량 선언 대신 인스턴스 크기가 전부입니다.

> 기존에 이미 배포된 환경이 있다면 새로 만들지 말고
> `./deploy doctor --capture`로 현재 상태를 gateway.yaml로 역생성하세요 —
> [update.md](update.md)의 온보딩 절.

## 명령 — 일상에서 쓰는 것들

```bash
./deploy configure   # 설정 마법사 — 항목별 확인(Enter=유지) → 끝에서 "지금 배포?"
./deploy bootstrap   # [최초 1회] 신규 설치 — 인프라·시크릿·이미지·Cognito 전체
./deploy apply       # 업데이트/동기화 — yaml 그대로 배포 (render 내장)
                     #   --plan: 변경 미리보기 (eks=helm server dry-run+
                     #   라이브 diff, ecs=terraform plan, compose=산출물 diff)
./deploy doctor      # 상태·드리프트 점검  (--capture: 기존 배포 → yaml 역생성)
./deploy client-values  # 직원에게 전달할 env 4줄 출력 — client-setup.md §0 의 값
```

생명주기는 **`bootstrap`(신규) → `apply`(업데이트 반복) → `teardown`(삭제)**입니다.
기존 배포를 이어받는 경우는 bootstrap 대신 `doctor --capture`로 yaml을
역생성한 뒤 `apply`로 들어옵니다. 최초 yaml 생성은 `./deploy init`.
아래는 고급 명령 — `configure`/`apply`가 내부에서 다 하므로 일상 경로에선 불필요합니다:

```bash
./deploy init        # [최초 1회] 대화형으로 gateway.yaml 생성
./deploy validate    # [고급] yaml 검증만
./deploy render      # [고급] 산출물만 생성해서 확인
./deploy bootstrap --plan  # 신규 설치 단계 미리보기 — 아무것도 안 바뀜
./deploy apply --plan   # 변경 계획만 보기 — 아무것도 안 바뀜
./deploy teardown    # [위험] 배포 삭제 — 소유 범위별 (환경 이름 입력으로 확인)
./deploy upgrade eks --to 1.<minor>  # [위험] EKS 클러스터/애드온 홉 단위
                     # 업그레이드 — apply는 버전을 바꾸지 않음
                     #   --plan: 홉 계획·애드온 버전만 출력
```

`teardown`은 apply가 소유하는 것만 지웁니다 — compose는 컨테이너+네트워크
(`--purge`로 볼륨=DB까지), ecs는 terraform이 만든 AWS 리소스 전부
(deletion protection이 켜진 티어는 해제 후 삭제), eks는 helm release만 —
`--infra`를 붙이면 terraform env의 인프라(EKS·Aurora·VPC 등)까지 삭제합니다.
자세한 절차는
[update.md](update.md#배포-삭제-teardown) 참조.

## 이 도구가 하는 것 / 하지 않는 것

**하는 것 (gateway.yaml → apply):**

| backend | 관리 범위 |
|---|---|
| compose | 앱 스택 전체(9개 서비스+Caddy) · 도메인 자동 TLS · IP allowlist · OIDC · 알림 · 이미지 핀 · 마이그레이션 자동 선행 |
| ecs | **인프라까지 전부**: VPC/Aurora/ElastiCache/ALB/ECS/Cognito/Secrets/ECR · 이미지 build+push · 마이그레이션 task · private ALB |
| eks | `bootstrap`: tfstate · env terraform(EKS/Aurora/ElastiCache/Cognito/Secrets/IRSA) · SM 시드 · 이미지 build+push · Cognito admin → 이후 helm release 관리(apply와 동일 범위) |

ECS는 자체 Cognito 풀을 사용합니다. SAML IdP는 `infra.cognito_saml`로 연결하고
팀과 역할을 결정할 그룹 클레임은 `oidc.groups_claim`으로 설정합니다. [ADFS SAML 배포](adfs-sso.md)

**하지 않는 것:**

- **eks의 인프라 갱신 자체** — `bootstrap`은 최초 생성까지 담당하지만, 이후 클러스터 버전·노드 변경 같은 인프라 변경은 terraform env가 소유합니다(`apply`는 helm/앱 레이어 + feature 인프라 보강만)
- **ecs 기존 인프라 편입** — 모듈이 전부 새로 만듭니다(기존 VPC/RDS 재사용 불가)
- **bi_insight 기본 비활성** — `features.bi_insight: true` 시 managed overlay(`module.agentcore_runtime`)가 인프라를 프로비저닝 (`web_search` Gateway는 `features.web_search`로 자동)
- **DB 롤백** — 마이그레이션은 forward only
- **유실된 시크릿 복구** — `.env`의 `VIRTUAL_KEY_ENCRYPTION_KEY`를 잃으면 발급된 Virtual Key 전부 재발급 필요

## 시작 전에

**배포 호스트가 없다면 먼저** [install-prereq.md](install-prereq.md) — Deployer
IAM role → EC2 생성 → Bedrock 액세스 확인 → `bootstrap-ec2.sh`(도구 일괄 멱등
설치) → 사용 중인 원격 IDE 서버를 감지해 그에 맞는 `pkill` 재시작 명령을
출력하는 것까지의 전 과정입니다.

이 디렉토리는 배포 도구도 함께 담고 있습니다 — `./deploy`가 진입점이고
`deploy.sh`(실행) → `deploy/`(파이썬 패키지) 순입니다. 처음 쓰기 전에:

```bash
python3 -m venv docs/NDS/.venv
docs/NDS/.venv/bin/pip install -r docs/NDS/deploy/requirements.txt
```

(`deploy`는 venv가 없으면 시스템 python3로 폴백 — PyYAML과 `rich`가 있으면 동작)

## 문서 목록 — 나는 누구인가

**🛠️ 나는 게이트웨이를 설치/업데이트하는 사람** (배포 담당·인프라)

| 문서 | 내용 |
|---|---|
| [install-prereq.md](install-prereq.md) | **0단계 — 배포 호스트 준비** (EC2·deployer role·Bedrock 확인·bootstrap-ec2) |
| [install-compose.md](install-compose.md) | **t0 — EC2 1대 설치** |
| [install-ecs.md](install-ecs.md) | **t1/t2 — ECS Fargate 신규 구축** |
| [install-eks.md](install-eks.md) | **t2/t3 — EKS 신규 구축(bootstrap)·기존 배포 온보딩·업데이트** |
| [domain.md](domain.md) | **도메인 획득 → 배포에 등록** — backend별 자동화 정도 |
| [ADFS SAML 배포](adfs-sso.md) | `gateway.yaml`로 SAML IdP와 앱 클라이언트 설정 관리 |
| [AD 그룹 및 사용자 등록](ad-sync.md) | RP Identifier, ACS URL, `custom:groups`, 로그인 후 역할·팀 검증 |
| [Cognito Managed Login](managed-login.md) | 호스팅 로그인 화면 브랜딩 설정과 검증 |
| [update.md](update.md) | 업데이트·롤백·기존 배포 온보딩(capture) |
| [upgrade-eks-cluster.md](upgrade-eks-cluster.md) | EKS 컨트롤플레인/애드온/Aurora 업그레이드 |
| [gateway-yaml.md](gateway-yaml.md) | 설정 파일 전체 항목 설명서 |

**🧭 나는 게이트웨이를 운영하는 사람** (관리자)

| 문서 | 내용 |
|---|---|
| [영문 관리자 상세 가이드](guides/admin-guide.en.html) | 로그인, 관리자 메뉴 13개, 단계별 작업 절차와 dev 캡처 74장. 브라우저에서 열 때 `screenshots` 폴더를 HTML과 함께 둡니다. |
| [한국어 관리자 UI 가이드](guides/README.md) | 로그인 및 비밀번호 재설정, 기능별 화면과 진입 경로, 모델 관리 |
| [영문 운영·배포 인수인계 가이드](op-guide/README.md) | 설치부터 운영, 변경, 장애 복구까지의 절차와 체크리스트 |
| [operations.md](operations.md) | **운영 런북** — 직원 등록·오픈 전 체크리스트·크로스계정 라우팅·Marketplace |
| [관측 및 알림](observability.md) | CloudWatch·SNS, Alertmanager, 비용 원장 이상 알림 |
| [dr-runbook.md](dr-runbook.md) | **재해 복구** — 백업 복원·시크릿 유실·RTO/RPO·리허설 |
| [loadtest.md](loadtest.md) | **부하테스트** — mock-bedrock SSE 동시성·executor-HPA 검증·원복 절차 |
| [chat-agent.md](chat-agent.md) | **admin-chat-agent (BI 챗)** — 기본 미배포, 옵트인 활성화 절차 |
| [client-admin.md](client-admin.md) | **클라이언트 관리자용** — env 4줄 산출·인스톨러 빌드/배포·서버측 진단 |

**💻 나는 이 게이트웨이에 내 PC를 연결하는 직원**

| 문서 | 내용 |
|---|---|
| [client-setup.md](client-setup.md) | **시작은 여기** — Claude Code · Cowork · Codex (한 페이지, 3×3 매트릭스) |
| [cowork/](cowork/README.md) | Cowork 상세 — macOS 수동·Windows 인스톨러/수동/제거 |

**통합 및 저장소 유지보수**

| 문서 | 내용 |
|---|---|
| [AICM 통합 계약](aicm/README.md) | `/aicm/v1` 인증·권한·예산 동작과 OpenAPI 계약 |
| [upstream 동기화](upstream-sync.md) | upstream 변경을 `nds/dev`에 병합하는 절차 |

**설계 및 이전 배포 참고**

- [배포 설계 결정](decisions.md) · [R7 외부 비용 대조 설계](cost-reconciliation-r7.md)
- [`install-eks.sh` 배포의 전환 참고](upgrade-eks-legacy.md) · [NDS 전환 변경 기록](upstream-diff/README.md) · [리뷰 기록](review/README.md)

## 지원 클라이언트와 모델

- Claude Code, Cowork(Claude Desktop), Codex
- 모델은 기본 **`global.*` inference profile** — 특정 리전에 고정되지 않아
  어느 리전에서 배포해도 동일하게 동작합니다
- 크로스계정 라우팅은 두 축이 있습니다 — **클라이언트별**
  (`model.routing_profiles.account_role_arn`, /apps → 크로스 계정 라우팅 카드)
  과 **모델별** (`model.model_aliases.account_role_arn`, /models 수정).
  절차는 [operations.md](operations.md) §3·§3.5
- Cowork는 https가 필수 — `domain.mode: none`이면 동작하지 않습니다
- 직원 PC 연결 절차는 [client-setup.md](client-setup.md) — 게이트웨이 배포가
  끝나면 이 문서 하나만 직원에게 전달하면 됩니다
