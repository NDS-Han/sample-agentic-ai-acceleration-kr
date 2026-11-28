# NDS Gateway 문서

**한국어** · [English](README.en.md)

Claude Code, Cowork, Codex를 연결하는 NDS Gateway의 배포·운영 문서입니다.
환경은 `deployment/gateway.yaml`에 선언하고 `./deploy`로 배포합니다.

| 지금 할 일 | 시작할 문서 |
|---|---|
| 관리자 콘솔에서 계정·모델·정책 관리 | [관리자 상세 가이드](guides/admin-guide.ko.md) |
| 게이트웨이 신규 설치 또는 운영 인수 | [운영·배포 인수인계](op-guide/README.ko.md) |
| 내 PC에서 Claude Code·Cowork·Codex 사용 | [직원 PC 빠른 시작](client-setup.md) |
| 장애 조사 또는 복구 | [운영 런북](operations.md) · [관측·알림](observability.md) · [재해 복구](dr-runbook.md) |

## 문서 목록 — 나는 누구인가

### 배포 담당자

| 작업 | 문서 |
|---|---|
| 배포 호스트 준비 | [사전 준비](install-prereq.md) |
| 배포 대상 선택 | [EKS](install-eks.md) · [ECS](install-ecs.md) · [Compose](install-compose.md) |
| 기존 환경 편입, 업데이트, 롤백 | [변경 관리](update.md) · [EKS/Aurora 업그레이드](upgrade-eks-cluster.md) |
| 도메인과 인증 연결 | [도메인·TLS](domain.md) · [ADFS 설정](adfs-sso.md) · [그룹 매핑·등록 검증](ad-sync.md) · [Cognito 로그인 브랜딩](managed-login.md) |
| 배포 설정 확인 | [gateway.yaml 레퍼런스](gateway-yaml.md) |

### 운영 관리자

| 작업 | 문서 |
|---|---|
| 콘솔 기능 및 스크린샷 | [관리자 상세 가이드](guides/admin-guide.ko.md) · [콘솔 문서 목록](guides/README.md) |
| 설치·운영 인수인계 | [운영·배포 가이드](op-guide/README.ko.md) |
| 직원 등록과 모델 라우팅 | [운영 런북](operations.md) · [모델 관리](guides/models.md) |
| 관측, 복구, 부하 검증 | [관측·알림](observability.md) · [재해 복구](dr-runbook.md) · [부하 검증](loadtest.md) |
| 선택 기능과 직원 배포 | [BI Insight 배포](chat-agent.md) · [클라이언트 관리자](client-admin.md) |

### 직원과 애플리케이션 개발자

- [직원 PC 빠른 시작](client-setup.md)에서 운영체제와 앱을 선택합니다.
- [Cowork 설치 및 제거](cowork/README.md)에서 macOS 프로파일과 Windows 설치기 절차를 확인합니다.
- [AICM 통합 계약](aicm/README.ko.md)에서 전용 API와 인증 방식을 확인합니다.

### 설계 및 이전 배포 참고

- 유지보수 · [upstream 동기화](upstream-sync.md) · [배포 설계 결정](decisions.md)
- 기술 참고 · [Alertmanager 추가 수신자 예시](alerting-receivers.md) · [R7 외부 비용 대조 설계](cost-reconciliation-r7.ko.md)
- 변경 비교 · [upstream e3a39757 이후 NDS Gateway 변경 내역](upstream-diff/README.md)
- 이전 기록 · [레거시 EKS 전환](upgrade-eks-legacy.md) · [리뷰 기록](review/README.md)

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
