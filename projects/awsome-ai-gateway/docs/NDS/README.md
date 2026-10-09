# NDS 배포 가이드 — gateway.yaml 기반 통합 배포

이 문서는 **어느 환경에서든** LLM Gateway를 배포하는 새로운 표준 절차입니다.
기존 `docs/us-llm-gateway/` 는 특정 사이트(US, us-west-2, 단일 AWS 계정 구조)에
고정된 운영 문서이고, 이 디렉토리의 절차는 리전·계정·규모에 관계없이 동작합니다.

## 핵심 개념 — 세 개의 명령

```
./deploy init      →  대화형으로 gateway.yaml 생성 (배포의 source of truth)
./deploy render    →  배포 대상에 맞는 산출물 생성
./deploy doctor    →  배포 상태·드리프트 점검 (업데이트 전후에도 사용)
```

모든 설정은 **`deployment/gateway.yaml` 한 파일**에 있습니다. 변경은 이 파일을
고치고 다시 render/apply 하는 것 — 환경별로 스크립트를 찾아 실행하는 방식이
아닙니다.

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
| [gateway-yaml.md](gateway-yaml.md) | 설정 파일 전체 항목 설명서 |
| [update.md](update.md) | 업데이트·롤백 절차 |
| [decisions.md](decisions.md) | 이 구조를 선택한 이유 (설계 배경) |

## 지원 클라이언트와 모델

- Claude Code, Cowork(Claude Desktop), Codex
- 모델은 기본 **`global.*` inference profile** 사용 — 특정 리전(us/apac)에
  고정되지 않아 어느 리전에서 배포해도 동일하게 동작합니다.
- Codex/Cowork의 Mantle 크로스어카운트 라우팅은 배포 계정별 설정이 필요합니다
  (`model.routing_profiles.account_role_arn` — 관리 UI 또는 DB 시드에서 설정).
