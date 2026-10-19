# t1/t2 설치 — AWS ECS Fargate

**대상**: 소규모(100~300명, t1) / 표준(300~1000명, t2) 운영 배포.
AWS 관리형으로 자동 복구·수평 확장·무패치 컴퓨트를 얻고, Kubernetes 운영 부담은 없습니다.

완성되면: VPC + ALB + ECS Fargate 서비스 6개 + Aurora PostgreSQL + ElastiCache Valkey +
Cognito(SSO) + Secrets Manager — 전부 Terraform 선언형.

> 이 문서는 `deploy` CLI 기준입니다. 수동 terraform 사용도 가능합니다 — `deploy render` 가
> 만드는 tfvars 를 그대로 넘기면 됩니다(§3 의 명령 참조).

---

## 0. 준비물

> **배포 호스트가 없다면** [install-prereq.md](install-prereq.md) 먼저 — 배포 EC2
> 생성·deployer IAM role·Bedrock 접근 확인·`bootstrap-ec2.sh`(아래 도구 전부 멱등
> 설치)·세션 재시작(`pkill -f 'vscode-server|cursor-server'`)까지 다룹니다.

| 항목 | 요구 |
|---|---|
| AWS 계정 | Bedrock Claude 모델 접근 + infra 생성 권한 (VPC/ECS/RDS/ElastiCache/Cognito/SM/ECR/ALB/ACM/Route53) |
| 자격증명 | `aws sts get-caller-identity` 가 되는 IAM principal (deploy 머신) |
| 도구 | terraform ≥ 1.9, awscli v2, docker (이미지 빌드/push), python 3.11+ (`deploy` CLI용) — `bootstrap-ec2.sh` 가 일괄 설치 |
| Terraform state | S3 bucket (+ 권장: DynamoDB lock table). 없으면 §1.5 에서 생성 |
| 리전 | `global.*` inference profile 이 라우팅되는 리전 아무거나 — ap-northeast-2 검증됨 |
| Bedrock 모델 | 대상 리전에서 `global.anthropic.*` (또는 리전 프로필) 사용 허용 — 기본값은 global 프로필 |
| 도메인(선택) | Route53 hosted zone — 없어도 시작 가능(HTTP), 나중에 추가 가능 |

### 비용 감각 (목표치, 실제는 사용량 의존)

- t1: ~$200/월 — Fargate 6서비스 + ALB + NAT 1 + Aurora Serverless v2(1~4 ACU) + Valkey 단일노드
- t2: ~$500/월 — NAT·캐시(replica)·task Multi-AZ, task 수 2배. **DB는 Aurora Serverless 단일 writer** — Multi-AZ 아님

### 용량·오토스케일링

- **gateway-proxy**(SSE 진입점): desired → `max_count`(기본 ×4)까지 CPU 60%·
  Mem 70% target-tracking으로 자동 확장. t1은 2→8task, t2는 4→16task.
  **admin-api**도 ×2까지 확장. scale-in은 ALB 드레인(300s) 후 종료라 진행 중
  스트림이 보호됩니다.
- **싱글톤 계약**: scheduler·cost-recorder·notification은 스트림 consumer/
  cron 중복 방지로 1개 고정 — autoscaling도 min=max=1 핀입니다.
- **DB/캐시 크기**: tier 프리셋이 기본 — `configure`의 "DB/캐시 크기를 직접
  정하시겠습니까?" 분기(또는 `infra.db`/`infra.cache` 직접 선언)로 min/max
  ACU·instance_class·cache mode·node_type·노드/샤드 수를 세부 조정합니다.
  매 `apply`에 반영되므로 설치 후 변경도 같은 경로입니다.
- **캐시 비용 = 노드 수 × node_type**: Aurora ACU와 함께 최대 비용 손잡이입니다.
  `num_clusters`(non-cluster), `num_node_groups`/`replicas_per_node_group`
  (cluster)이 노드 수를, `node_type`이 단가를 결정합니다 — 토폴로지 키가
  mode와 어긋나면 스키마가 거부합니다(terraform이 조용히 무시하는 걸 방지).
- **DB 커넥션 예산**: task가 늘면 pool 수요(30conn/task worst, gw 60)도
  비례해 늡니다. `validate`가 최악치가 Aurora `max_acu`(~225conn/ACU)의
  80%를 넘지 않는지 검사하고, 정상 수요가 `min_acu` 바닥을 넘으면 경고합니다 —
  `deploy.sizing`/`infra.db`를 키우기 전에 `deploy validate`를 돌리세요.
  그 이상의 탄력이 필요하면 eks prod가 쓰는 **RDS Proxy**가 구조적 해법입니다
  (pool 다중화, 별도 비용).

## 1. 저장소와 gateway.yaml

```bash
git clone <이 저장소> && cd projects/awsome-ai-gateway
./deploy init
```

마법사에서:

```
배포 대상: ecs
규모 티어 : t1 (소규모) 또는 t2 (표준 Multi-AZ)
도메인   : 없으면 none → 나중에 route53-acm 으로 전환 가능
```

### 1.5 Terraform state backend (최초 1회)

ECS 경로는 terraform remote state 를 씁니다. **`./deploy bootstrap` 이 bucket/table
이름을 묻고 `bootstrap-tfstate.sh` 로 자동 생성**합니다 — 아래 수동 절차는
bootstrap 을 쓰지 않고 `apply` 로만 진행할 때 필요합니다:

```bash
aws s3 mb s3://llm-gateway-tfstate-<account-id> --region <region>
aws s3api put-bucket-versioning --bucket llm-gateway-tfstate-<account-id> \
  --versioning-configuration Status=Enabled
# (권장) lock table
aws dynamodb create-table --table-name llm-gateway-tflock \
  --attribute-definitions AttributeName=LockID,AttributeType=S \
  --key-schema AttributeName=LockID,KeyType=HASH --billing-mode PAY_PER_REQUEST
```

버킷/테이블 이름이 규칙과 다르면 gateway.yaml 에 명시:

```yaml
deploy:
  tfstate_bucket: my-tfstate
  tfstate_table: my-tflock
```

### 1.6 신규 설치 — `./deploy bootstrap` (권장)

```bash
./deploy bootstrap          # yaml 없으면 init 마법사부터
./deploy bootstrap --plan   # 단계 미리보기
```

대화형으로 ① tfstate 이름 → ② AZ 복수 선택(최소 2, 서브넷 CIDR 자동 생성) →
③ Cognito suffix(`<region>-auth-<account>`)+ `Claude_` 그룹 → ④ bedrock 허용
ARN + web-search 를 묻고 `infra.*` 로 기록한 뒤, tfstate 생성 → apply 를
연속 실행합니다. 비대화형은 `--yes` + yaml 의 `infra.*` 선언
([gateway-yaml.md](gateway-yaml.md#infra--신규-설치-인프라-입력)).
**이미 tfstate 가 있거나 단계별로 진행하려면** 아래 §2~§3 (`apply`) 경로로
바로 가도 됩니다.

## 2. (선택) Render — tfvars 미리 확인

> `apply`가 내부에서 render를 이미 수행하므로 이 단계는 **생략 가능**입니다.
> 생성물을 눈으로 확인하고 싶을 때만 실행합니다.

```bash
./deploy render          # deployment/gen/<env>/ecs/ 에 생성
```

산출물:

| 파일 | 내용 |
|---|---|
| `terraform.tfvars` | `environments/gateway-ecs` 에 넘길 변수 — 티어에서 파생된 DB/캐시/사이징 포함 |
| `backend.hcl` | `terraform init -backend-config` 용 state 위치 |

> tfvars 는 **생성물입니다 — 직접 고치지 마세요**. 변경은 gateway.yaml → `render` → `apply`.
> 단, apply 가 발견하는 동적 값(도메인 없을 때 ALB DNS 기반 URL)은
> `extra-vars.json` 에 기록되어 다음 render 에 자동 합쳐집니다.

## 3. Apply — 배포

```bash
./deploy apply --plan   # 변경 계획만 확인 (아무것도 안 바뀜)
./deploy apply          # plan 출력 → 확인 프롬프트(y) → 적용
./deploy apply --yes    # 확인 생략 (CI/자동화)
```

`apply` 는 기본적으로 terraform plan 결과를 먼저 보여주고 확인을 요청합니다 —
기존 환경에 무엇이 바뀌는지 보고 결정할 수 있습니다.

내부 순서 (각 단계 멱등 — 실패해도 재실행하면 이어감):

1. `terraform apply -target=ECR repos` — 이미지 push 대상 repo 먼저 생성
2. `docker build + push` — 6개 이미지를 `<tag>` 로 ECR 에 push
3. `terraform apply` — VPC/Aurora/ElastiCache/Cognito/ALB/task-def 순서대로.
   **migration task 가 apply 중 실행**되고(init SQL + app DB 유저 + alembic head),
   서비스는 그 완료를 기다립니다 — 실패 시 apply 가 여기서 멈춥니다
4. (domain=none) 발견된 ALB DNS 를 `extra-vars.json` 에 기록하고 한 번 더 apply —
   admin-ui `NEXTAUTH_URL` 등이 실제 접속 주소를 가리킴
5. `services-stable` 대기 — circuit breaker 가 불건강 배포를 자동 롤백

끝나면 URL 이 출력됩니다:

```
gateway_url:   http://llm-gateway-acme-123.ap-northeast-2.elb.amazonaws.com:8000
admin_ui_url:  http://...:3000
admin_api_url: http://...:8080
```

수동으로 하려면:

```bash
terraform -chdir=deployment/terraform/environments/gateway-ecs init \
  -backend-config=$PWD/deployment/gen/<env>/ecs/backend.hcl
terraform -chdir=deployment/terraform/environments/gateway-ecs apply \
  -var-file=$PWD/deployment/gen/<env>/ecs/terraform.tfvars
```

## 4. 확인 — `deploy doctor`

```bash
./deploy doctor
```

점검 항목: 산출물 존재 → AWS 자격 → `terraform plan` no-op(드리프트) →
서비스 running/desired → migration task exitCode → ALB 엔드포인트 `/health` 응답.

> ⚠️ **첫 요청 전 — 라우팅 프로필 확인 (필수)**: DB 시드의 `claude-code`/`cowork`
> 라우팅 행에는 샘플 크로스계정 ARN(`arn:aws:iam::333…/2222…`)이 들어 있습니다.
> 단일 계정 배포면 `account_role_arn`을 비워(in-account) 두세요 — 그대로 두면
> 없는 계정으로 AssumeRole을 시도해 요청이 전부 실패합니다. 이 값은 admin UI에서
> 못 고치고 **SQL**로 바꿉니다 — 절차는 [operations.md](operations.md) §3
> (backend별 실행법 포함).

## 5. Cognito 사용자 생성 + admin 로그인

OIDC 는 Cognito 가 제공합니다(모듈이 user pool 생성). 첫 관리자:

```bash
POOL_ID=$(terraform -chdir=deployment/terraform/environments/gateway-ecs output -raw cognito_user_pool_id)
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username admin@example.com --user-attributes Name=email,Value=admin@example.com
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username admin@example.com --group-name ClaudeAdmin
# ⚠️ 관리자도 팀 그룹에 넣으세요 — ClaudeAdmin 만 있으면 VK 발급이 403 입니다
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username admin@example.com --group-name Claude_<dept>_<team>   # infra.cognito_groups 에 선언한 팀 그룹
```

`admin_ui_url` 로 접속 → Cognito hosted UI 로 리다이렉트 → 로그인.
직원을 계속 추가하는 절차는 [operations.md](operations.md) 참조.

## 6. 도메인을 나중에 붙이기

`domain.mode: none` → `route53-acm` 전환은 선언 변경 한 번:

```yaml
domain:
  mode: route53-acm
  name: gw.example.com
  zone_id: Z0123456789ABC   # Route53 hosted zone
```

```bash
./deploy render && ./deploy apply
```

같은 apply 에서 ACM wildcard cert 발급 → DNS 검증 레코드 → 443 리스너 →
`gateway./admin./admin-api.` Route53 alias → Cognito 콜백 추가까지 완료됩니다.

> zone 을 쓸 수 없으면 `domain.name` 만 넣으세요 — `cert_validation_records`
> 출력의 레코드를 수동 DNS 에 등록 → cert 가 ISSUED 되면
> `certificate_arn` 을 tfvars 에 추가해 재apply 하면 HTTPS 가 열립니다.

## 7. 업데이트

```bash
# gateway.yaml 에서 images.tag 만 변경
./deploy apply          # tag 가 바뀌면 migration 이 자동으로 먼저 실행된다
./deploy doctor         # plan no-op + 서비스 healthy 확인
```

롤백: `images.tag` 를 이전 값으로 → `./deploy apply`. DB 마이그레이션은
forward-only 라 이전 태그의 alembic head 가 현재보다 낮아도 스키마는 유지됩니다
(코드-스키마 호환성은 릴리스 노트로 확인).

## 8. 배포 삭제

```bash
./deploy teardown   # plan -destroy 미리보기 → 환경 이름 입력 → terraform destroy
```

VPC/Aurora/ElastiCache/ALB/ECS/Cognito/Secrets/ECR 이 전부 지워지고,
`web_search`/`pricing_lambda`를 켰다면 AgentCore Gateway와 LiteLLM Lambda도
함께 정리됩니다. t2 등 `db_safeguards`가 켜진 티어는 CLI가 deletion
protection을 해제한 뒤 삭제하므로 **최종 스냅샷 없이 완전 삭제** — 보존이
필요하면 먼저 `aws rds create-db-cluster-snapshot`을 실행하세요.
상세는 [update.md](update.md#배포-삭제-teardown) 참조.

## 9. 아직 안 되는 것 (솔직한 목록)

| 기능 | 상태 |
|---|---|
| `web_search` | AgentCore Gateway 를 먼저 만들고 `agentcore_gateway_url` 을 tfvars 에 추가 + `routing_profiles` DB 행 필요 |
| `pricing_lambda` | LiteLLM Lambda 배포는 이 경로 범위 밖 |
| `bi_insight` | `agentcore_runtime` 모듈 배선 미구현 — env main.tf 에 추가 필요 |
| `observability` | CloudWatch logs + container insights 는 기본. OTel/Grafana 스택은 미구현 |
| `cloudfront-temp` | ECS 경로에 CloudFront 없음 → `none` (HTTP) 또는 `route53-acm` |
| 외부 OIDC | ECS 경로는 Cognito 생성이 기본 — 외부 IdP 는 모듈 변수 직접 주입으로만 가능 |

## 10. public → private 전환

```yaml
network:
  mode: private
```

→ `render` + `apply` 하면 ALB 가 internal 로 바뀌고 private subnet 으로 이동합니다.
**선행조건**: 사용자가 ALB 에 닿는 경로(VPN/DX/사내망)가 있어야 합니다 —
없으면 전환 즉시 전원 차단됩니다. doctor 의 endpoint 체크가 그 시점부터 실패하는
것으로 확인됩니다(정상 — 사내에서 확인).
