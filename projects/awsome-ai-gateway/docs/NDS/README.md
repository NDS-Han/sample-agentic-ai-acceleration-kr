# NDS 배포 가이드 — gateway.yaml 기반 통합 배포

이 문서는 **어느 환경에서든** LLM Gateway를 배포하는 새로운 표준 절차입니다.
기존 `docs/us-llm-gateway/` 는 특정 사이트(US, us-west-2, 단일 AWS 계정 구조)에
고정된 운영 문서이고, 이 디렉토리의 절차는 리전·계정·규모에 관계없이 동작합니다.

## 핵심 개념 — 명령 책임

**일상에서 쓰는 명령은 3개**입니다:

| 명령 | 역할 |
|---|---|
| `./deploy configure` | **설정 마법사** — 현재 값을 기본값으로 항목별 확인 → 저장 → "지금 배포?" 까지. 처음엔 `init` 이 같은 역할 |
| `./deploy apply` | **yaml 그대로 배포** — 파일을 직접 고쳤거나 CI/스크립트에서. render를 내부에서 수행 + plan 미리보기 → 확인 → 적용 → doctor |
| `./deploy doctor` | **상태 확인** — 헬스·드리프트 점검. `--capture` = 기존 배포를 yaml 로 역생성 |

고급/디버깅 명령(일상 경로에선 불필요 — `configure`/`apply` 가 내부에서 다 합니다):

| 명령 | 역할 |
|---|---|
| `./deploy render` | 산출물만 생성해서 눈으로 확인 (`apply` 가 자동으로 render 함) |
| `./deploy validate` | yaml 문법 검증만 (`configure`/`apply` 도 시작 시 검증) |
| `./deploy init` | 최초 1회 — yaml 이 없을 때의 configure |

모든 설정은 **`deployment/gateway.yaml` 한 파일**이 원본입니다. 두 입구가 있습니다:
대화형(`configure`)과 파일 직접 편집(`vi` 후 `apply`) — 어느 쪽이든 최종 적용은
같은 apply 경로를 탑니다.

> 이 디렉토리는 배포 도구도 함께 담고 있습니다 — `deploy.sh` (실행 진입점),
> `deploy/` (파이썬 패키지). 루트의 `./deploy` 는 `docs/NDS/deploy.sh` 로
> 전달하는 래퍼입니다. 처음 쓰기 전에 의존성을 설치하세요:
>
> ```bash
> python3 -m venv docs/NDS/.venv
> docs/NDS/.venv/bin/pip install -r docs/NDS/deploy/requirements.txt
> ```
>
> (`deploy` 는 venv 가 없으면 시스템 python3 으로 폴백합니다 — PyYAML 과
> `rich` 가 있으면 바로 동작합니다.)

## 어떤 경로로 배포할까

| 규모 | 배포 대상 (`deploy.target`) | 티어 | 특징 |
|---|---|---|---|
| 평가·소규모(~50명) | `compose` | t0 | 단일 EC2/VM + Docker Compose. 가장 단순·저렴 |
| 소규모(100~300명) | `ecs` | t1 | ECS Fargate + Aurora Serverless v2. **권장 표준** |
| 표준(300~1000명) | `ecs` 또는 `eks` | t2 | Multi-AZ, replica 캐시 |
| 대규모(1000명+) | `eks` | t3 | 현재 EKS 프로덕션 구성 |

## 문서 목록

| 문서 | 내용 |
|---|---|
| [install-compose.md](install-compose.md) | **t0 — 단일 호스트 설치** (검증 완료) |
| [install-ecs.md](install-ecs.md) | **t1/t2 — ECS Fargate 설치** (권장 표준 경로) |
| [install-eks.md](install-eks.md) | **t2/t3 — 기존 EKS 배포 온보딩·업데이트** |
| [gateway-yaml.md](gateway-yaml.md) | 설정 파일 전체 항목 설명서 |
| [update.md](update.md) | 업데이트·롤백 절차 |
| [decisions.md](decisions.md) | 이 구조를 선택한 이유 (설계 배경) |

## 지원 클라이언트와 모델

- Claude Code, Cowork(Claude Desktop), Codex
- 모델은 기본 **`global.*` inference profile** 사용 — 특정 리전(us/apac)에
  고정되지 않아 어느 리전에서 배포해도 동일하게 동작합니다.
- Codex/Cowork의 Mantle 크로스어카운트 라우팅은 배포 계정별 설정이 필요합니다
  (`model.routing_profiles.account_role_arn` — 관리 UI 또는 DB 시드에서 설정).
