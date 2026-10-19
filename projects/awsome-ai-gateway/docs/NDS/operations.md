# 운영 런북 — 직원 등록 · 오픈 전 체크리스트 · 라우팅 프로필 · 신형 모델 구독

배포가 끝난 뒤 **반복되는 운영 작업**을 모읍니다. 설치 자체는
install-{compose,ecs,eks}.md, 변경은 [update.md](update.md) 참조.

## 1. 직원 등록 (Cognito 사용자 + 그룹)

`bootstrap`은 **첫 관리자 1명**만 만듭니다(`ClaudeAdmin` + 첫 팀 그룹 자동 배정).
직원은 운영자가 계속 추가합니다 — 세 단계:

```bash
POOL_ID=$(terraform -chdir=<terraform env dir> output -raw cognito_user_pool_id)
# ecs: deployment/terraform/environments/gateway-ecs
# eks: deployment/terraform/environments/llm-gateway-<env>

# 1) 사용자 생성 — 임시 비밀번호는 이메일로 발송 (SUPPRESS 를 쓰면 콘솔에만 표시)
aws cognito-idp admin-create-user --user-pool-id $POOL_ID \
  --username user@example.com \
  --user-attributes Name=email,Value=user@example.com Name=email_verified,Value=true

# 2) 그룹 배정 — 둘의 의미가 다릅니다:
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username user@example.com --group-name Claude_<dept>_<team>   # 팀 그룹 — 필수
aws cognito-idp admin-add-user-to-group --user-pool-id $POOL_ID \
  --username user@example.com --group-name ClaudeAdmin          # 관리자만 — 일반 직원 금지
```

| 그룹 | 의미 | 안 넣으면 |
|---|---|---|
| `Claude_<dept>_<team>` (bootstrap 의 `infra.cognito_groups`가 만든 것) | 팀 소속 — VK 발급의 전제 | 로그인은 되는데 **VK 발급 403** |
| `ClaudeAdmin` | admin-ui(`/models`·예산 등) 접근 | 관리 UI 403 — 의도된 제한 |

3) **동기화** — admin-ui ▸ 사용자/팀 관리의 **Cognito 동기화** 버튼을 누르면
Cognito 사용자·그룹이 게이트웨이 조직 트리로 반영됩니다(팀·예산·비용 집계
단위). 동기화 전에는 사용자가 조직에 안 보일 뿐 로그인·VK는 곧바로 됩니다.

> 그룹 목록 확인: `aws cognito-idp list-groups --user-pool-id $POOL_ID`.
> 팀 그룹이 없으면 `infra.cognito_groups`(ecs) 또는 env tfvars `cognito_groups`
> (eks)에 선언 후 apply — 그룹 생성은 terraform 소유입니다.

## 2. 직원 오픈 전 하드닝 체크리스트

설치 편의로 입구가 넓게 열려 있습니다. **직원에게 열기 전** 아래를 전부
확인하세요 — `./deploy doctor` 와 함께 마지막 게이트입니다:

- [ ] **OIDC 로그인 확인** — admin-ui 접속 시 Cognito hosted UI로
  리다이렉트되는지. `oidc` 블록이 비어 있으면 dev-login(역할만 고르면 서명
  없는 관리자 토큰)이 켜진 상태입니다 — **절대 오픈 금지**. eks에서
  `DEV_LOGIN_ENABLED`가 false인지도 확인.
- [ ] **`network.allowed_cidrs` 확정** — 설치용 관리자 `/32`를 지우고 직원
  출구 대역으로 교체 → `apply`. eks/eks·ecs 에서 이 값이 곧 ALB SG 인그레스로
  reconcile됩니다(**SG를 직접 편집하지 마세요 — 컨트롤러가 지웁니다**).
  "대역이 없다(재택·동적 IP)"면 `0.0.0.0/0`으로 열지 말고 NAT/프록시로
  출구를 고정한 뒤 그 대역을 넣으세요.
- [ ] **admin 콘솔만 더 좁히기** — eks: `network.admin_internal: true`
  (admin-ui/admin-api Ingress만 internal — S2S VPN 전제) 또는 env values의
  per-ingress `inbound-cidrs`를 관리자 대역으로. compose/ecs에서는 admin
  경로를 관리자 대역으로 좁히는 것을 권장.
- [ ] **도메인/TLS** — `domain.mode: none`(HTTP) 상태로 오픈하면 Cowork는
  동작 안 하고 자격증명이 평문으로 지납니다 → [domain.md](domain.md)로
  도메인을 붙인 뒤 오픈 권장.
- [ ] **모델 허용 목록** — `infra.bedrock_allowed_model_arns`가 열린 의도대로인지
  확인 (비우면 profile 기본).
- [ ] **VK TTL** — `oidc.vk_ttl_hours` 기본값이 정책에 맞는지.
- [ ] **백업** — compose: install-compose.md §6 크론 백업 설정. ecs/eks:
  Aurora 스냅샷 정책 확인.
- [ ] **`.env`/시크릿 보관** — `VIRTUAL_KEY_ENCRYPTION_KEY` 백업됐는지
  (유실 시 발급 VK 전부 무효).

## 3. 라우팅 프로필 — in-account vs 크로스계정

모델 별칭(alias)은 DB 시드·migration이 자동으로 넣지만, `model.routing_profiles`
(클라이언트별 호출 경로)는 **운영 데이터라 배포가 건드리지 않습니다**. 시드에
들어 있는 `claude-code`/`cowork` 행은 개발 조직의 샘플 크로스계정 ARN
(`arn:aws:iam::333…`/`2222…`)을 가리킵니다.

> ⚠️ **admin UI/API로는 이 값을 못 고칩니다** — 라우팅 API는 목록 조회와
> web-search 토글만 제공합니다. `account_role_arn`·`backend`·`default_model`
> 변경은 **SQL**로 합니다.

- **단일 계정 배포** (대부분): `account_role_arn`/`external_id`를 비우세요
  (NULL = in-account, 태스크/파드 자신의 IAM 자격증명으로 직접 호출).
  그대로 두면 없는 계정으로 AssumeRole을 시도해 요청이 전부 실패합니다 —
  배포 직후 첫 번째로 확인할 항목. `cowork`의 `default_model`도 비워야
  사용자 모델 선택이 살아납니다(시드가 `cowork-opus`로 고정).
- **다른 계정의 Bedrock으로 보내기** (계정 분리·비용 격리·규제): 대상 계정에
  AssumeRole 가능한 역할을 만들고(`llm-gateway-<env>` 서비스 자격증명 신뢰 +
  ExternalId), `account_role_arn`·`external_id`·`region`을 그 값으로 UPDATE
  하면 됩니다 — 아래 SQL에서 NULL 대신 실제 값을 넣으면 됩니다.
- `codex` 행은 시드부터 in-account(`account_role_arn` NULL)라 별도 조치가
  필요 없습니다.

**적용 SQL** (backend 공통):

```sql
UPDATE model.routing_profiles
   SET backend          = 'invoke',
       account_role_arn = NULL,
       external_id      = NULL,
       default_model    = NULL
 WHERE client IN ('claude-code','cowork');
-- 확인 — account_role_arn/external_id/default_model 이 비어있어야 합니다
SELECT client, backend, account_role_arn, external_id, default_model
  FROM model.routing_profiles WHERE client IN ('claude-code','cowork');
```

> 변경 후 **최대 5분** 기다리세요 — 게이트웨이가 라우팅을 Redis에 300초
> 캐시합니다. 서비스 재시작은 의미 없습니다(캐시는 파드가 아니라 ElastiCache).

### backend별 실행법

**compose** — postgres 컨테이너에 직접:

```bash
docker compose -f deployment/gen/<env>/docker-compose.yml \
  exec postgres psql -U gateway -d gateway -v ON_ERROR_STOP=1 \
  -c "UPDATE model.routing_profiles SET backend='invoke', account_role_arn=NULL,
      external_id=NULL, default_model=NULL
      WHERE client IN ('claude-code','cowork');"
```

**eks** — 클러스터 안 임시 postgres 파드(배포 머신은 VPC 밖이라 직접 못 붙습니다):

```bash
PGHOST=$(terraform -chdir=deployment/terraform/environments/llm-gateway-<env> \
  output -raw rds_proxy_endpoint)
PGPASSWORD=$(aws secretsmanager get-secret-value --secret-id /llm-gateway/<env>/db \
  --query SecretString --output text | jq -r .password)
kubectl -n <namespace> run psql --rm -it --restart=Never --pod-running-timeout=5m \
  --image=public.ecr.aws/docker/library/postgres:16 \
  --env="PGHOST=$PGHOST" --env="PGUSER=gateway" --env="PGDATABASE=gateway" \
  --env="PGPASSWORD=$PGPASSWORD" --command -- psql -v ON_ERROR_STOP=1 \
  -c "UPDATE model.routing_profiles SET backend='invoke', account_role_arn=NULL,
      external_id=NULL, default_model=NULL
      WHERE client IN ('claude-code','cowork');"
```

**ecs** — migration 태스크를 일회성으로 실행(이미지에 psql + DB 자격증명 내장):

```bash
cd deployment/terraform/environments/gateway-ecs
CLUSTER=$(terraform output -raw ecs_cluster_name)
TASKDEF=$(terraform output -raw migration_task_definition_arn)
SUBNETS=$(terraform output -json private_subnet_ids | jq -c .)
SG=$(terraform output -raw task_security_group_id)
aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE \
  --task-definition "$TASKDEF" \
  --network-configuration "{\"awsvpcConfiguration\":{\"subnets\":$SUBNETS,\"securityGroups\":[\"$SG\"],\"assignPublicIp\":\"DISABLED\"}}" \
  --overrides "$(cat <<'EOF'
{"containerOverrides":[{"name":"migration",
 "environment":[{"name":"FIX_SQL","value":"UPDATE model.routing_profiles SET backend='invoke', account_role_arn=NULL, external_id=NULL, default_model=NULL WHERE client IN ('claude-code','cowork');"}],
 "command":["sh","-c","psql \"$DB_MASTER_URL\" -v ON_ERROR_STOP=1 --echo-all -c \"$FIX_SQL\""]}]}
EOF
)"
# 결과 — 태스크 종료 후 migration 로그에서 UPDATE 2 확인
# (로그 그룹 = /ecs/<project>-<env>/migration — project 기본값 llm-gateway)
aws logs tail /ecs/llm-gateway-<env>/migration --since 10m
```

## 4. 신형 모델 — Bedrock Marketplace 구독

신형 Anthropic 모델은 **Model access 신청이 아니라 AWS Marketplace 구독**입니다.
모델을 게이트웨이에 등록해도 계정에 구독이 없으면 호출이
`AccessDeniedException … aws-marketplace:Subscribe` 로 떨어집니다.

```bash
# 증상 확인 — 모델 호출이 AccessDenied 일 때
aws bedrock get-foundation-model-availability --region <리전> \
  --model-id <모델 id> --query authorizationStatus --output text
# AUTHORIZED 가 아니고 use case form 도 이미 냈다면 → Marketplace 구독 필요
```

절차: 콘솔 **Bedrock ▸ Model catalog → 해당 모델 → Subscribe** (IAM에
`aws-marketplace:Subscribe` 권한 필요 — deployer role에 없으면
`AWSMarketplaceSellerProductsFullAccess` 계열 정책을 임시 부여).
구독 전파까지 ~2분. 이후 inference profile 호출이 바로 됩니다.

상세 실측(US dev 계정 재현 기록)은 레거시
`docs/us-llm-gateway/ops/8-Q-marketplace.md` 참조.
