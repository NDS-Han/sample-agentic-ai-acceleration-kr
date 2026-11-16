# 배포 모델 — upstream 대비 변경 전체

upstream의 배포는 **사이트 고정 스크립트**(`docs/us-llm-gateway/` 절차 문서,
`update-scripts/*.sh`, `install-eks.sh`, EC2 기준 수작업 values 편집)였다.
NDS는 이를 **`gateway.yaml` 단일 선언 파일 + `./deploy` 통합 도구**로 교체했다.

## 1. 선언적 배포 도구 (`docs/NDS/deploy/`)

upstream에 없던 전체 신규 서브시스템 (~11k LOC + 테스트 478).

| 구성 | 역할 |
|---|---|
| `cli.py` | 명령 디스패치 — init/configure/validate/render/bootstrap/apply/doctor/teardown/client-values |
| `schema.py` | `gateway.yaml` 스키마 — 필드 검증·경고·SchemaError 수집 |
| `tiers.py` | `deploy.capacity_tier`(사용자 수 밴드)→ pod/task 수·용량 프리셋 전개, `deploy.sizing` 덮어쓰기 |
| `render/` | 대상별 산출물 렌더링 — `compose.py`(docker-compose+.env+Caddyfile), `eks`(helm values), `ecs`(tfvars) |
| `bootstrap.py` | 신규 설치 — 인프라·시크릿·이미지·Cognito 전체 프로비저닝 |
| `eks_apply.py` / `ecs_apply.py` | 업데이트 경로 — helm apply, terraform plan/apply, `--plan` 미리보기 |
| `capture.py` | `doctor --capture` — 기존 배포를 gateway.yaml로 역생성(helm values+kubectl 라이브 대조, compose .env 병합) |
| `doctor.py` | 상태·드리프트 점검 — 준비성·위험 감지 |
| `websearch.py` / `chatagent.py` / `pricinglambda.py` | 기능별 프로비저너 — boto3로 AgentCore Runtime·Lambda 생성·동기화 |
| `tests/` | 478 tests — 매 리뷰 라운드의 회귀 고정 |

명령 수명주기: `init/configure`(yaml 작성) → `bootstrap`(신규 1회) →
`apply`(업데이트 반복, render 내장) → `doctor`(점검) → `teardown`(삭제).
`./deploy`는 `docs/NDS/deploy.sh`로 위임하는 4줄 래퍼다.

## 2. 배포 대상 — 3 경로

| 대상 | 티어 | upstream 대비 |
|---|---|---|
| `compose` | t0 고정 | 신규 — 단일 호스트 Docker Compose, Caddy 인입점 |
| `ecs` | t1/t2 | **신규** — ECS Fargate + Aurora + ElastiCache 전체를 terraform으로 생성 |
| `eks` | t2/t3 | 기존 EKS 경로를 NDS 수명주기에 편입 — `bootstrap`/`apply`/`doctor`/`teardown` 통합 |

`capacity_tier` 사용자 수 밴드가 eks pod min/max·ecs task 수를 같은
밴드로 전개 — 용량 선언의 정본이 gateway.yaml.

## 3. Terraform 모듈 (`deployment/terraform/modules/`)

upstream 대비 신규 12종: `vpc`, `eks-fargate`, `ecs-gateway`,
`aurora-postgresql`, `elasticache-valkey`, `cognito`, `irsa`,
`alb-controller`, `external-secrets`, `alerting`,
`bedrock-invocation-logging`, `body-logging`, `agentcore-runtime`.
environments: `gateway-ecs`, `llm-gateway-dev`, `llm-gateway-prod`.

## 4. 도메인·TLS 자동화

- `domain.mode`: `none` / `route53-acm` / `selfsigned-temp`(EKS 전용,
  도메인 없이 임시 https) / `cloudfront-temp`
- `route53-acm`: ACM 인증서 발급·검증 CNAME·ALB 레코드·Cognito 콜백
  도메인 부착을 apply가 자동 처리
- compose는 Caddy 단일 인입점, eks는 ALB+인증서, 자가서명은
  `selfsigned-temp`로 개발용 https

## 5. 업데이트·수렴 안전장치 (upstream에 없던 것)

- **`apply --plan`**: eks=helm server dry-run+라이브 diff, ecs=terraform
  plan, compose=산출물 diff — 변경 전 미리보기
- **`doctor --capture`**: 라이브 helm values·kubectl 상태·.env를 읽어
  gateway.yaml 역생성 — 기존 배포 온보딩
- **소유 증명**: `_release_manifest_names`로 우리 release 리소스만
  캡처/teardown 대상 인정 — 라벨 위조·교차 타깃 오인 방지(27차 리뷰)
- **teardown**: apply 소유분만 삭제 — compose(컨테이너+네트워크,
  `--purge` 볼륨), ecs(terraform 리소스 전부, deletion protection
  티어는 해제 후), eks(helm release, `--infra`로 인프라까지)
- **declared-off 수렴**: `merge_env`가 선언에서 빠진 관리 키를 .env에서
  제거(WEB_SEARCH_ENABLED→AGENTCORE_* 동반 수렴 등)
- **pending-teardown 마커**: `.pending-provisioner-teardown` — teardown
  실패해도 추적 증거 보존

## 6. 관련 문서 (docs/NDS/)

`install-compose/ecs/eks-prereq.md`, `gateway-yaml.md`(스키마),
`domain.md`, `update.md`(업데이트·teardown·capture), `ad-sync.md`,
`observability.md`, `dr-runbook.md`, `upstream-sync.md`,
`review/`(29라운드 리뷰 레포트 + AMM + PR 기록).
