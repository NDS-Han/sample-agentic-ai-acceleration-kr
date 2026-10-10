# NDS 배포 가이드 — gateway.yaml 기반 통합 배포

LLM Gateway(Claude Code · Cowork · Codex 지원)를 **어느 AWS 계정·리전에서든**
같은 방법으로 배포하는 표준 절차입니다. 기존 `docs/us-llm-gateway/`는 특정
사이트(US, us-west-2)에 고정된 운영 문서이고, 이 디렉토리는 범용입니다.

핵심 아이디어: **설정은 `deployment/gateway.yaml` 한 파일**이 원본이고,
`./deploy` 하나가 그 파일을 읽어 배포 대상에 맞는 모든 것을 만듭니다.
환경별 스크립트를 찾아 돌리는 방식이 아닙니다.

## 어떤 경로로 배포할까

| 상황 | 배포 대상 | 문서 | 뭐가 생기나 |
|---|---|---|---|
| 평가·POC·소규모(~50명) | `compose` (t0) | [install-compose.md](install-compose.md) | EC2 1대에 Docker Compose로 전체 스택 |
| 소규모~표준(100~1000명) | `ecs` (t1/t2) | [install-ecs.md](install-ecs.md) | ECS Fargate + Aurora + ElastiCache — **인프라까지 전부 생성** |
| 기존 EKS 배포 관리·대규모 | `eks` (t2/t3) | [install-eks.md](install-eks.md) | 기존 클러스터의 helm release를 이어받아 관리 |

> 기존에 이미 배포된 환경이 있다면 새로 만들지 말고
> `./deploy doctor --capture`로 현재 상태를 gateway.yaml로 역생성하세요 —
> [update.md](update.md)의 온보딩 절.

## 명령 — 일상에서 쓰는 건 3개

```bash
./deploy configure   # 설정 마법사 — 항목별 확인(Enter=유지) → 끝에서 "지금 배포?"
./deploy apply       # yaml 그대로 배포 — 파일을 직접 고쳤거나 CI에서. render 내장
./deploy doctor      # 상태·드리프트 점검  (--capture: 기존 배포 → yaml 역생성)
```

최초 생성은 `./deploy init` (yaml이 없을 때의 configure). 아래는 고급 명령 —
`configure`/`apply`가 내부에서 다 하므로 일상 경로에선 불필요합니다:

```bash
./deploy init        # [최초 1회] 대화형으로 gateway.yaml 생성
./deploy validate    # [고급] yaml 검증만
./deploy render      # [고급] 산출물만 생성해서 확인
./deploy apply --plan   # 변경 계획만 보기 — 아무것도 안 바뀜
./deploy teardown    # [위험] 배포 삭제 — 소유 범위별 (환경 이름 입력으로 확인)
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
| eks | helm release 관리: 이미지 태그 · ingress host/CIDR · OIDC 계약 · 알림 · feature 플래그 · migration hook · 자동 롤백 |

**하지 않는 것:**

- **eks 인프라 자체** — EKS 클러스터·Fargate·Aurora·VPC·IRSA·ESO는 기존 terraform env가 소유합니다(이 도구는 helm/앱 레이어만)
- **ecs 기존 인프라 편입** — 모듈이 전부 새로 만듭니다(기존 VPC/RDS 재사용 불가)
- **AgentCore 리소스** — `web_search`·`bi_insight`의 AgentCore Gateway/Runtime은 수동 프로비저닝 (us-east-1 구조적 제약)
- **외부 IdP on ecs** — ecs 경로는 Cognito를 직접 생성하므로 yaml의 oidc 값은 무시됩니다(`required_group`만 반영)
- **DB 롤백** — 마이그레이션은 forward only
- **유실된 시크릿 복구** — `.env`의 `VIRTUAL_KEY_ENCRYPTION_KEY`를 잃으면 발급된 Virtual Key 전부 재발급 필요

## 시작 전에

이 디렉토리는 배포 도구도 함께 담고 있습니다 — `./deploy`가 진입점이고
`deploy.sh`(실행) → `deploy/`(파이썬 패키지) 순입니다. 처음 쓰기 전에:

```bash
python3 -m venv docs/NDS/.venv
docs/NDS/.venv/bin/pip install -r docs/NDS/deploy/requirements.txt
```

(`deploy`는 venv가 없으면 시스템 python3로 폴백 — PyYAML과 `rich`가 있으면 동작)

## 문서 목록

| 문서 | 내용 |
|---|---|
| [install-compose.md](install-compose.md) | **t0 — EC2 1대 설치** (EC2 생성·IAM·도구·배포 전 과정 런북) |
| [install-ecs.md](install-ecs.md) | **t1/t2 — ECS Fargate 신규 구축** |
| [install-eks.md](install-eks.md) | **t2/t3 — 기존 EKS 배포 온보딩·업데이트** |
| [update.md](update.md) | 업데이트·롤백·기존 배포 온보딩(capture) |
| [gateway-yaml.md](gateway-yaml.md) | 설정 파일 전체 항목 설명서 |
| [decisions.md](decisions.md) | 이 구조를 선택한 이유 (설계 배경) |

## 지원 클라이언트와 모델

- Claude Code, Cowork(Claude Desktop), Codex
- 모델은 기본 **`global.*` inference profile** — 특정 리전에 고정되지 않아
  어느 리전에서 배포해도 동일하게 동작합니다
- Codex/Cowork의 Mantle 크로스어카운트 라우팅은 배포 계정별 설정이 필요합니다
  (`model.routing_profiles.account_role_arn` — 관리 UI 또는 DB 시드에서 설정)
- Cowork는 https가 필수 — `domain.mode: none`이면 동작하지 않습니다
