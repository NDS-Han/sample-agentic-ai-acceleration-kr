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
| 6 | Cognito | `adminApi.adminBootstrap.emails[0]` 사용자 생성 + `ClaudeAdmin` 그룹 배정 (임시 비밀번호 1회 표시) |

후속 수동 단계: `smoke-test.sh --env <env>` 앱 경로 스모크, admin UI에서 모델
alias/라우팅 시드(install-guide §4 — 운영 데이터라 자동화 대상 아님).

- **env scaffold 주의**: 생성된 `terraform.tfvars`의 `eks_cluster_version`·
  `eks_addon_versions`·`tags`는 템플릿 기본값 — 조직에 맞게 검토한 뒤
  재실행해야 합니다 (bootstrap이 여기서 멈춥니다). bi 챗(`enable_chat_agent`)은
  NDS 배포 대상이 아니라 scaffold 에서 자동으로 false 로 생성됩니다
- **비대화형/CI**: `--yes` 는 wizard 없이 yaml 선언값만 씁니다 — `infra.*` 를
  미리 채워 두면 됩니다 ([gateway-yaml.md](gateway-yaml.md#infra--신규-설치-인프라-입력))
- 신규 설치 후 업데이트는 경로 B/C와 같은 `apply` 흐름입니다

## 경로 B — 기존 배포 온보딩

대상: 이미 `install-eks.sh` + helm + terraform 으로 배포된 EKS 환경.
이 경로는 **기존 배포를 이어받아** helm 릴리스만 관리합니다 — EKS 클러스터·Aurora·
ElastiCache·Cognito·IRSA·ESO 같은 인프라는 기존 terraform env 가 계속 소유합니다.

## 개념 정리 — `env` 이름 vs `size_tier`

이 둘은 직교하는 별개 개념입니다:

| 개념 | 의미 | 예 |
|---|---|---|
| `env` / `tf_env_dir` | **어느 인프라 스택인가** — DB·Redis·IRSA·Cognito·시크릿 경로(`/llm-gateway/<env>/*`)를 만든 terraform env | `llm-gateway-dev`, `llm-gateway-prod` |
| `size_tier` | **그 스택의 크기** — 우리가 옵션으로 선택 | `t2`, `t3` |

`dev`는 크기가 아니라 스택 식별자입니다 — 같은 t3로 dev/prod 두 스택을 띄울
수 있습니다. `--env-dir`에는 **실제로 그 인프라를 만든 디렉토리**를 지정하면
되고 이름이 꼭 `*-dev`일 필요는 없습니다. 아래 예의 `llm-gateway-dev`는
기존 배포가 그 이름으로 만들어졌기 때문에 쓰는 것입니다.

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

capture notes 가 알려주는 주의점:

- **서비스별 이미지 태그가 다른 경우**(예: proxy 1.0.86 / api 1.0.89 / ui 1.0.168) —
  `images.tag` 는 하나뿐이라, 첫 `apply` 에서 **전 서비스가 같은 태그로 통일**됩니다.
  의도된 통일이면 그대로, 아니면 태그를 먼저 정리하세요.
- `domain.name` 은 `gateway.<base>`/`gateway-dev.<base>` 에서 base 만 추출합니다.
  다음 `render` 가 `gateway./admin./admin-api.<name>` 네이밍을 씁니다 — 기존
  `gateway-dev.*` 패턴과 다르면 URL 이 바뀌니 확인하세요.

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
./deploy apply --plan          # helm upgrade --dry-run (변경 없음, render 내장)
./deploy apply                 # plan 출력 → 확인(y) → helm upgrade --install
```

> 다른 클러스터 context가 잡혀 있을 수 있으면 `--context <ctx>`를 붙여
> 대상을 명시하세요 — apply가 helm/kubectl 전부에 그 context를 전달합니다.

### 내부 동작

1. values 레이어 = `values.yaml`(차트 기본) → `values-eks-fargate-<env>[.local].yaml`
   → `gen/<env>/eks/values.yaml`(우리 오버레이, 마지막이 이긴다)
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

eks 경로는 인증서·DNS가 사람 소유라 ecs 처럼 한 apply 로 끝나지 않습니다:

```bash
# 1. ACM 에서 인증서 발급 (gateway./admin./admin-api.<domain> 3개 SAN 또는 wildcard)
#    + DNS 검증 — 사내 도메인이면 사내 DNS 에 검증 레코드를 등록
# 2. env values 에 cert 추가
vi deployment/charts/llm-gateway/values-eks-fargate-<env>.local.yaml
#    ingress.annotations."alb.ingress.kubernetes.io/certificate-arn": arn:aws:acm:...
```

```yaml
# 3. gateway.yaml — host 재작성은 여기서 일어난다
domain:
  mode: route53-acm
  name: <domain>            # → gateway./admin./admin-api.<domain> 3개 host 생성
                            # + adminUi.nextauthUrl 갱신 (Cognito callback 도 맞출 것)
```

```bash
# 4. 적용 후 DNS — 레코드 생성은 수동 (레거시 11-route53-cname.sh 와 같은 일)
./deploy apply
kubectl get ingress -n llm-gateway -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.loadBalancer.ingress[0].hostname}{"\n"}{end}'
#    → 각 host 를 해당 ALB DNS 로 가리키는 CNAME 을 Route53/사내 DNS 에 등록
```

주의: `domain.name`을 쓰면 host 이름이 **env 무관 규칙**(`gateway.`/`admin.`/
`admin-api.`)으로 바뀝니다 — `gateway-dev.<domain>` 같은 기존 이름을 유지해야
하면 `domain.name`을 비워두세요 (env values 의 host 가 그대로 쓰입니다).
hosted zone/도메인 자체의 획득·NS 위임은 모든 backend 에서 이 도구의 범위 밖입니다.

## 이 경로가 관리하는 것 / 하지 않는 것

| 관리함 (gateway.yaml → helm) | 관리하지 않음 (기존 terraform/스크립트 소유) |
|---|---|
| 이미지 태그(전 서비스 통일) | EKS 클러스터·Fargate profile |
| ingress host·inbound-cidrs·scheme | Aurora·ElastiCache·VPC |
| OIDC 계약(3개 서비스 동일) | Cognito user pool 자체 |
| notification provider·발신자 | IRSA role 생성(ARN 주입만) |
| WEB_SEARCH/body_logging 플래그 | AgentCore Gateway·Firehose 생성 |
| migration hook 실행 | ESO·SecretStore |

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
