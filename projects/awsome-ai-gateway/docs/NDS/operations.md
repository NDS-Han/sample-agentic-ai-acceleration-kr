# 운영 런북 — 직원 등록 · 오픈 전 체크리스트 · 라우팅 프로필 · 모델별 계정 라우팅 · 신형 모델 구독

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

## 3.5 모델별 계정 라우팅 — 모델 수정 화면에서 ARN 입력

§3의 라우팅 프로필은 **클라이언트(앱) 축**입니다 — "claude-code는 이 계정"처럼
앱 단위로 갈림길을 정합니다. 여기서 설명하는 것은 **모델 축** 오버라이드입니다 —
"이 모델 alias로 들어온 요청은 이 계정으로"를 모델 행에 직접 적습니다.

`model.model_aliases`의 `account_role_arn`/`external_id`/`region`(migration 0049,
전부 NULL 허용)이 그 자리입니다. §3과 달리 **관리 UI에서 편집됩니다** — SQL이
아니라 모델 관리 → 모델 수정 → 하단 **"라우팅 (선택)"** 섹션에서 입력합니다.

```
우선순위: model_aliases.account_role_arn (있으면) > routing_profiles[client] > in-account
적용 범위: native Bedrock invoke · Mantle · OpenAI-compatible · fallback 후보 · count_tokens
```

### 대상 계정에 롤 만들기 — UI보다 먼저 할 일

관리자가 ARN을 입력하는 것은 "화살표를 긋는 것"이고, 화살표 끝에 롤이 서
있어야 합니다. 게이트웨이는 `sts:AssumeRole(ARN)`을 호출할 뿐이므로:

- **같은 계정 롤**이면 운영자가 직접 만들면 됩니다 — 같은 계정 assume도
  크로스계정과 코드 경로가 완전히 동일해서 테스트 용도로도 씁니다.
- **외부 계정**이면 그 계정 관리자가 만들어야 합니다.

롤에 필요한 두 가지(패턴은 `deployment/scripts/provision_cowork_cross_account_role.sh`
참조 — cowork용이지만 trust/권한 구조가 그대로 범용입니다):

```json
// trust policy — 게이트웨이 실행 역할만 assume + ExternalId 조건
{
  "Effect": "Allow",
  "Principal": { "AWS": "<gateway 실행 역할 ARN>" },
  "Action": "sts:AssumeRole",
  "Condition": { "StringEquals": { "sts:ExternalId": "<임의의 공유 문자열>" } }
}
```

```json
// permission — 호출하려는 서비스에 맞게
bedrock:InvokeModel, bedrock:InvokeModelWithResponseStream, bedrock:CountTokens
(+ Mantle 모델이면 bedrock-mantle:*)
```

`<gateway 실행 역할 ARN>` 찾기:

```bash
# eks — IRSA (service account의 annotation)
kubectl -n llm-gateway get sa llm-gateway-gateway-proxy \
  -o jsonpath='{.metadata.annotations.eks\.amazonaws\.com/role-arn}'
# ecs — task role
terraform -chdir=deployment/terraform/environments/gateway-ecs \
  output -raw task_role_arn
# compose — EC2 인스턴스 프로필의 role
```

게이트웨이 쪽에도 `sts:AssumeRole` 권한이 대상 ARN에 걸려 있어야 합니다
(eks는 terraform `modules/irsa`의 role_arn 변수들, ecs는 task role 정책 —
배포 시 들어간 값이 우리 계정 롤이면 같은 계정 롤은 별도 조치 없이 됩니다).

### 관리 UI에서 설정

모델 관리 → 모델 수정 → "라우팅 (선택)":

| 필드 | 규칙 |
|---|---|
| AWS 계정 Role ARN | `arn:aws:iam::<12자리>:role/...` 형식 검증 |
| 리전 | **ARN을 넣으면 필수** — 대상 계정에서 호출할 리전 |
| External ID | 선택. 롤 trust의 `sts:ExternalId` 조건과 같은 값 |

- ARN을 지우고 저장하면 **트리플 전체 해제** — 다음 요청부터 클라이언트
  프로필 라우팅으로 복귀합니다.
- UI/API 경유 저장은 모델 캐시를 즉시 무효화합니다(§3 프로필의 5분 대기와
  다름). **직접 SQL로 바꾼 경우만** Redis `model:<alias>` 키를 지워야 합니다.

### 동작 특성 — §3 프로필 라우팅과 다른 점

- **fail-closed:** 모델에 ARN이 명시됐는데 AssumeRole이 실패하면 **503 에러**로
  끝납니다. 클라이언트 프로필 경로의 "조용히 in-account로 폴백"은 여기엔
  없습니다 — 모델 단위로 명시한 라우팅이 다른 계정으로 새면 비용이 잘못된
  계정에 청구되므로 차단합니다.
- 대상 계정에 **모델 접근 권한/Marketplace 구독이 있어야** 합니다 — 롤은
  있어도 그 계정이 모델을 못 쓰면 invoke 시 AccessDenied가 납니다(§4).
- External ID는 감사 로그에 마스킹돼 저장됩니다.

### 검증

```bash
# 1) 게이트웨이 파드에서 대상 롤이 실제로 assume 되는지 (EKS 예)
kubectl -n llm-gateway exec deploy/llm-gateway-gateway-proxy -- python3 -c \
  "import boto3;print(boto3.client('sts').assume_role(
    RoleArn='<대상 롤 ARN>',RoleSessionName='t',ExternalId='<external_id>'
  )['Credentials']['AccessKeyId'][:8])"

# 2) 모델로 호출 → 실패 시 gateway 로그에서 model_account_routing 관련 확인
kubectl -n llm-gateway logs deploy/llm-gateway-gateway-proxy --tail=200 \
  | grep -iE "model.*rout|assume|503"
```

모델 테이블에서 ARN이 설정된 모델은 `→ <마스킹된 계정>` 배지로 표시됩니다.

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
