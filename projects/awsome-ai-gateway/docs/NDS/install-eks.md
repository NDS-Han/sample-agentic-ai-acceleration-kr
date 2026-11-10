# EKS — 신규 설치(bootstrap) · 기존 배포 온보딩 · 업데이트

> **배포 호스트**: 모든 명령은 배포 EC2(또는 동등한 deploy 머신)에서 실행합니다.
> 호스트가 없으면 먼저 [install-prereq.md](install-prereq.md) — EC2 생성·deployer
> IAM role·Bedrock 접근 확인·`bootstrap-ec2.sh` 도구 설치·세션 재시작까지 다룹니다.

## 경로 A — 신규 설치 (`bootstrap`)

대상: 인프라가 아무것도 없는 계정/리전. 한 명령이 install-guide §3 전체를 실행합니다.

```bash
./deploy bootstrap          # yaml 없으면 init 마법사부터 — 배포 방식 선택
./deploy bootstrap --plan   # 단계 미리보기
```

실행 순서 (각 단계 멱등 — 실패 시 고치고 `bootstrap` 재실행하면 이어감):

| # | 단계 | 내용 |
|---|---|---|
| 0 | env scaffold | `deploy.tf_env_dir`(기본 `deployment/terraform/environments/llm-gateway-<env>`)이 없으면 `llm-gateway-dev` 템플릿에서 생성 → **tfvars 검토 후 재실행** |
| ★ | 인프라 wizard | 대화형 질문 — 답은 `gateway.yaml` 의 `infra.*` 에 기록됩니다:<br>① tfstate bucket/table 이름(기본값 제공, 생성 실패 시 이름 재입력 루프)<br>② 배포할 AZ 복수 선택(live 목록, 최소 2 — 서브넷 CIDR 자동 생성)<br>③ Cognito domain suffix(기본 `<region>-auth-<account>`)+ `Claude_` 그룹<br>④ bedrock 허용 ARN(기본=variables.tf 최신 또는 직접 입력)+ web-search |
| 1 | tfstate | `bootstrap-tfstate.sh`로 state bucket + lock table 보장 |
| 2 | env apply | terraform apply — EKS·VPC·Aurora·ElastiCache·Cognito·Secrets·IRSA (수십 분). wizard 답변은 managed overlay `-var-file` 로 내려갑니다 |
| 3 | 시크릿 시드 | `/<project>/<env>/app`(랜덤 4키 생성) · `/redis`(auth_token 복사) · `/db`(terraform 소유 — 검증만, **절대 덮어쓰지 않음**) |
| 4 | 이미지 | ECR repo 생성 + 서비스 6종 build/push (`images.tag`/`tags`로 핀 — scheduler는 admin-api 재사용) |
| 5 | helm | `apply`와 동일한 helm upgrade — 동적 output 주입 + feature 인프라 보강 |
| 6 | Cognito | `adminApi.adminBootstrap.emails[0]` 사용자 생성 + `ClaudeAdmin` + 첫 `Claude_*` 팀 그룹 배정 (임시 비밀번호 1회 표시) |

후속 수동 단계: `smoke-test.sh --env <env>` 앱 경로 스모크. 모델 alias·단가는
init SQL + alembic + LiteLLM pricing Lambda가 자동 시드합니다. **routing
profiles 는 SQL 로 확인·정규화**해야 합니다 — 시드의 크로스계정 ARN이 다른
계정에서는 AssumeRole 실패를 유발합니다 → [operations.md](operations.md#3-routing-profiles--크로스계정) §3.

- **env scaffold 주의**: 생성된 `terraform.tfvars`의 `eks_cluster_version`·
  `eks_addon_versions`·`tags`는 템플릿 기본값 — 조직에 맞게 검토한 뒤
  재실행해야 합니다 (bootstrap이 여기서 멈춥니다). bi 챗(`enable_chat_agent`)은
  NDS 배포 대상이 아니라 scaffold 에서 자동으로 false 로 생성됩니다.
  또한 `values-eks-fargate-<env>[.local].yaml`이 없으면 dev 파일을 복사합니다 —
  `adminBootstrap.emails`를 실제 운영자 이메일로 바꾸세요 (그대로 두면
  `admin@example.com` 사용자가 생성됩니다)
- **비대화형/CI**: `--yes` 는 wizard 없이 yaml 선언값만 씁니다 — `infra.*` 를
  미리 채워 두면 됩니다 ([gateway-yaml.md](gateway-yaml.md#infra--신규-설치-인프라-입력))
- 신규 설치 후 업데이트는 경로 B/C와 같은 `apply` 흐름입니다

## 경로 B — 기존 배포 온보딩

대상: 이미 `install-eks.sh` + helm + terraform 으로 배포된 EKS 환경.
이 경로는 **기존 배포를 이어받아** helm 릴리스만 관리합니다 — EKS 클러스터·Aurora·
ElastiCache·Cognito·IRSA·ESO 같은 인프라는 기존 terraform env 가 계속 소유합니다.

## 개념 정리 — `env` 이름 vs 용량 선언

이 둘은 직교하는 별개 개념입니다:

| 개념 | 의미 | 예 |
|---|---|---|
| `env` / `tf_env_dir` | **어느 인프라 스택인가** — DB·Redis·IRSA·Cognito·시크릿 경로(`/llm-gateway/<env>/*`)를 만든 terraform env | `llm-gateway-dev`, `llm-gateway-prod` |
| 용량 | **그 스택의 크기** — `size_tier`(문서용 라벨) + `capacity_tier`(사용자 수 밴드 → pod min/max) + `deploy.sizing`(개별 키) | `"3000"`, `min_replicas: 4` |

`dev`는 크기가 아니라 스택 식별자입니다 — 같은 t3로 dev/prod 두 스택을 띄울
수 있습니다. `--env-dir`에는 **실제로 그 인프라를 만든 디렉토리**를 지정하면
되고 이름이 꼭 `*-dev`일 필요는 없습니다. 아래 예의 `llm-gateway-dev`는
기존 배포가 그 이름으로 만들어졌기 때문에 쓰는 것입니다.

> **eks 에서 `size_tier` 는 문서용 라벨입니다** — eks 의 컴퓨트 크기는
> `capacity_tier`/`deploy.sizing`(또는 env values 의 기존 HPA)이 정합니다.
> `size_tier` 는 "이 환경이 어느 규모로 의도됐는지" 기록하는 값입니다.

## 사전 조건

- `kubectl` 로 클러스터 접근 (`kubectl get ns llm-gateway` 가 되는 context)
- `helm` ≥ 3.14
- (선택) terraform state 읽기 권한 — 있으면 DB/Redis/IRSA/cognito 값을 자동 주입.
  없으면 helm 만 업데이트하고 동적값은 기존 릴리스 값을 그대로 씁니다

## 1. 온보딩 — 현재 배포를 gateway.yaml 로

```bash
./deploy doctor --capture --target eks \
  --namespace llm-gateway --release llm-gateway \
  --env-dir deployment/terraform/environments/llm-gateway-<env>
#    ↑ 인프라를 만든 terraform env 디렉토리 (예: llm-gateway-dev)
# → deployment/gateway.captured-<env>.yaml

vi deployment/gateway.captured-<env>.yaml   # notes 의 빈칸 채우기
#   (대화형이 나으면: ./deploy configure --config deployment/gateway.captured-<env>.yaml --no-apply)
./deploy validate --config deployment/gateway.captured-<env>.yaml
mv deployment/gateway.captured-<env>.yaml deployment/gateway.yaml
```

capture 가 기록하는 것과 notes 가 알려주는 주의점:

- **서비스별 이미지 태그가 다른 경우** — 태그가 다르면 `images.tags` 맵에
  서비스별 핀으로 기록됩니다 (라이브 > values 명시 > 차트 appVersion 순).
  라이브가 values 와 다르면(수동 `set image`) 드리프트를 notes 로 알립니다.
- **기존 호스트 보존** — `gateway-dev.*` 처럼 파생 규칙과 다른 host 는
  `domain.hosts` 에 기록돼 다음 apply 에도 그대로 유지됩니다.
- **용량 역산** — 라이브 HPA min/max·`BEDROCK_THREAD_POOL_SIZE`·
  `redis.poolSize` 를 읽어 밴드와 정확히 일치하면 `capacity_tier` 로,
  아니면 `deploy.sizing` 실측으로 기록합니다.
- `domain.name` 은 `gateway.<base>`/`gateway-dev.<base>` 에서 base 만 추출합니다 —
  hosts 가 기록되면 네이밍은 유지되고, hosts 가 없으면 다음 render 가
  `gateway./admin./admin-api.<name>` 규칙을 씁니다.
- **역산으로 복원되는 항목** — `domain.zone_id`(Route53 최장 접미사 매칭),
  `deploy.tfstate_bucket/table`(init 된 env 의 `.terraform/terraform.tfstate`),
  `infra.bedrock_allowed_model_arns`/`mantle_regions`/`cognito_domain_suffix`
  (`terraform.tfvars`). `tf_env_dir` 은 `--env-dir` → 기존 yaml →
  `llm-gateway-<env>` 표준 명명 순으로 자동 탐지합니다.
- **notes 로만 기록되는 것**(스키마에 표현 없음) — ESO ExternalSecret 이름
  (kubectl 로 실제 감지), Fargate profile 이름(terraform output 의
  cluster_name 으로 조회), RDS Proxy 사용 여부.
- **의도적으로 캡처하지 않는 것** — 시크릿 본체(SM 값은 절대 읽지 않음,
  ESO Secret 이름만 기록), eks `size_tier`(문서용 키라 라이브 신호가 없어
  t3 고정 — 스펙은 `capacity_tier`/`sizing` 이 실측으로 커버합니다).

## 2. 업데이트 — 변경 반영

> **여기는 앱(helm) 업데이트입니다.** EKS 컨트롤플레인/애드온/Aurora 엔진처럼
> terraform env 소유 의존성의 버전 업그레이드는
> [upgrade-eks-cluster.md](upgrade-eks-cluster.md)의 별도 런북을 따릅니다
> (`./deploy doctor`가 tfvars↔라이브 버전 skew를 HIGH로 알려줍니다).

```bash
./deploy configure   # 현재 설정을 기본값으로 항목별 확인 → 끝에서 "지금 배포?" y
                     # → render → plan → 확인 → apply 까지 자동 연결
```

또는 파일 직접 편집 경로:

```bash
vi deployment/gateway.yaml     # 예: images.tag 를 새 버전으로
./deploy apply --plan          # 변경 미리보기 — 아무것도 안 바뀜 (render 내장)
./deploy apply                 # plan 출력 → 확인(y) → helm upgrade --install
```

> 다른 클러스터 context가 잡혀 있을 수 있으면 `--context <ctx>`를 붙여
> 대상을 명시하세요 — apply가 helm/kubectl 전부에 그 context를 전달합니다.

`--plan` 이 보여주는 것 (helm ≥ 3.13, 미만이면 client dry-run 폴백):

- **server dry-run** — apiserver 가 라이브 release 와 실제로 검증한 결과를
  라이브 매니페스트와 정규화 비교해 **변경/추가/삭제 리소스 + unified diff**
- managed 인프라 terraform plan(yaml 소유 모듈만 — body_logging/IRSA/cognito 등)
- web_search·pricing_lambda 프로비저닝 상태
- 라이브를 못 읽으면 전체 덤프로 폴백합니다

### 내부 동작

1. values 레이어 = `values.yaml`(차트 기본) → env overlay →
   `gen/<env>/eks/values.yaml`(우리 오버레이, 마지막이 이긴다)
   - env overlay 탐색 순서(첫 발견이 이김):
     `values-eks-fargate-<env>.local.yaml` → `values-eks-<env>.local.yaml`
     → `values-eks-fargate-<env>.yaml` → `values-eks-<env>.yaml`
   - **라이브 값은 `values-eks-fargate-<env>.local.yaml`에 두세요** — git이
     무시하는(gitignore) 머신별 파일이라 실 계정·엔드포인트·이미지 태그를
     담아도 커밋되지 않고, 추적된 템플릿이 깨끗해 브랜치 전환을 막지 않습니다.
     `.local.yaml`이 있으면 추적본보다 우선 선택됩니다.
2. `--set` 동적값 = terraform output 에서 해석 (install-eks.sh 와 동일 계약):
   `global.imageRegistry`, `database.external.host`, `redis.external.host`,
   `*.serviceAccount.annotations.eks.amazonaws.com/role-arn`, cognito issuer,
   body-logging env
3. migration = 차트의 pre-upgrade hook Job — 자동 실행, 실패 시 upgrade 실패
4. helm3 `--atomic` / helm4 `--rollback-on-failure` + `--cleanup-on-fail` 자동 선택
5. domain=none 이면 live ingress 의 ALB hostname 을 `adminUi.nextauthUrl` 로 2차 주입

## 2.5 용량 조정 — `capacity_tier` / `deploy.sizing`

게이트웨이 복제본 수는 `gateway.yaml` 한 줄로 정합니다 — helm values 나
`.local.yaml` 을 직접 고칠 필요가 없습니다:

```yaml
deploy:
  capacity_tier: "3000"    # ~3,000명 밴드 → pod min 3 / max 6
  # sizing:                # 밴드 위 개별 키 override (선택)
  #   redis: { pool_size: 200 }
```

밴드 표와 산식(동시 스트림 ≈ 사용자×0.075, pod 당 ~180 슬롯)은
[gateway-yaml.md](gateway-yaml.md#deploycapacity_tier--사용자-수-밴드-ecseks)
참조. 개별 키: `gateway-proxy.{min_replicas,max_replicas,
bedrock_thread_pool_size}`, `redis.pool_size` — 지정한 키만 밴드 위에 덮습니다.

- 밴드를 바꾸면 `gatewayProxy.autoscaling.enabled=true` + min/max 가 렌더됩니다.
- **밴드도 sizing 도 없으면** autoscaling·poolSize 키를 방출하지 않아 env
  values 의 기존 HPA/pool 이 그대로 유지됩니다(채택 배포의 기본 동작).
- Redis 총 커넥션 ≈ `pool_size × WORKERS(2) × max_replicas` — 노드
  maxclients 예산을 validate 가 검사합니다.
- 변경 적용: `./deploy apply --plan`(HPA diff 확인) → `./deploy apply`.

> **`gen/<env>/eks/values.yaml` 은 생성물입니다 — 직접 고치지 마세요.**
> 기존 `values-eks-fargate-<env>.yaml` 도 그대로 둡니다 — 우리 오버레이가
> 마지막 레이어라서 필요한 키만 덮어씁니다.

## 3. 상태 확인

```bash
./deploy doctor    # release 상태, 클러스터 버전 skew(live vs tfvars 선언),
                   # deployment readiness, migration job, 관리 키 drift,
                   # terraform plan drift
```

## 4. 도메인을 나중에 붙이기

`zone_id`가 있으면 인증서·DNS·Cognito 콜백·`nextauthUrl`까지 apply 한 번으로
끝납니다 — 전부 멱등이라 이미 있으면 찾아 씁니다:

```yaml
# gateway.yaml
domain:
  mode: route53-acm
  name: <domain>
  zone_id: <Route53 hosted zone id>   # 없으면 인증서·레코드만 수동 (아래 참조)
```

```bash
./deploy apply
# 자동: *.<domain> ACM 인증서 요청 → zone 에 검증 CNAME → ISSUED 후 443 리스너
#       → gateway./admin./admin-api.<domain> A alias upsert
#       → gen tfvars 의 콜백/로그아웃 URL 반영(terraform apply -target=module.cognito)
#       → adminUi.nextauthUrl = https://admin.<domain>
```

주의:

- `domain.name`을 쓰면 host 이름이 **env 무관 규칙**(`gateway.`/`admin.`/
  `admin-api.`)으로 바뀝니다 — `gateway-dev.<domain>` 같은 기존 이름을 유지해야
  하면 `domain.hosts` 에 실제 호스트를 명시하세요 (capture 가 자동 기록).
- `zone_id`가 없으면(사내 DNS 경로) 인증서 발급·검증과 host 레코드만 수동입니다 —
  절차는 [domain.md](domain.md) §2-1.
- 콜백 반영의 `module.cognito` 타깃 apply는 `deploy.tf_env_dir`이 가리키는
  terraform env에서 실행됩니다 — Cognito 풀 소유 env와 일치하는지 확인하세요
  (기본 `deployment/terraform/environments/llm-gateway-<env>`).
- hosted zone/도메인 자체의 획득·NS 위임은 모든 backend 에서 이 도구의 범위 밖입니다.

## 이 경로가 관리하는 것 / 하지 않는 것

| 관리함 (gateway.yaml → helm) | 관리하지 않음 (기존 terraform/스크립트 소유) |
|---|---|
| 이미지 태그(`images.tag` + 서비스별 `images.tags` 핀) | EKS 클러스터·Fargate profile |
| **용량 — HPA min/max·`BEDROCK_THREAD_POOL_SIZE`·`redis.poolSize`** (capacity_tier/sizing 선언 시) | Aurora·ElastiCache·VPC |
| ingress host·inbound-cidrs·scheme | Cognito user pool 자체 |
| OIDC 계약(3개 서비스 동일) | IRSA role 생성(ARN 주입만) |
| notification provider·발신자 | AgentCore Gateway·Firehose 생성 |
| WEB_SEARCH/body_logging 플래그 | ESO·SecretStore |
| migration hook 실행 | |

인프라 변경이 필요하면 해당 terraform env(`environments/llm-gateway-<env>`)에서
`terraform plan/apply` — doctor 가 그쪽 drift 도 같이 봅니다.

## 배포 삭제

```bash
./deploy teardown             # helm release(앱)만 삭제 — 인프라는 남음
./deploy teardown --infra     # 앱 + terraform env 인프라 전부 삭제
```

`--infra`는 `deploy.yaml`의 `env_dir`이 가리키는 terraform env에서
`plan -destroy` 미리보기 → 환경 이름 입력 확인 → helm uninstall --wait →
`terraform destroy` 순서로 진행합니다. 앱을 먼저 내려 ALB controller가 만든
리소스가 정리된 뒤 인프라를 지웁니다. 상세 주의사항은
[update.md](update.md#배포-삭제-teardown) 참조.
