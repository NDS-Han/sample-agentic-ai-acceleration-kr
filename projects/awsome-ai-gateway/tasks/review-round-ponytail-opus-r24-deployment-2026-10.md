# R24 — deployment 표면 리뷰 라운드 1 (kiro/opus, ponytail)

범위: `deployment/charts/llm-gateway/`(templates·values 4종·tests), `deployment/terraform/`(env 3 + module 12),
`deployment/scripts/`, `deployment/tui/`, Compose 렌더러(`docs/NDS/deploy/render/`), 정합 참조 `deployment/docs/`.
가정 부하: 운영 EKS Fargate(HPA 최대 30 gateway pod, 장시간 SSE), 배포는 `rebuild-image.sh`(라이브 SHA 태그 경로)와
`install-eks.sh`(문서상 정식 경로) 두 갈래.

## 이번 라운드에서 직접 실행한 검증

| 항목 | 결과 |
|---|---|
| `helm lint` + `helm template` (base / dev / prod / prod+loadtest) | 4개 모두 렌더 성공 |
| 차트 테스트 `pytest deployment/charts/llm-gateway/tests` | 27 passed |
| TUI 테스트 `pytest deployment/tui/tests` (rich/questionary/pytest 임시 venv) | 100 passed |
| `bash -n` 전 스크립트 / `shellcheck -S warning` | 구문 OK. 경고는 SC2088(bootstrap-ec2.sh:130), SC2034(사용 안 하는 변수)뿐 |
| `terraform init -backend=false` + `validate` (dev / prod / gateway-ecs) | 3개 모두 valid. prod는 `-lockfile=readonly`에서 **init 실패**(N5) |
| `terraform fmt -check -recursive modules` | 3개 파일 실패(N6) |
| **라이브 dev 클러스터 읽기 전용 확인** (`helm list/history/get values`, `kubectl get secret/pdb/hpa/sa/ingress`, `aws iam list-roles`) | rev 222 deployed, SHA 태그(`eed423d6` 등), 아래 M5·S1·S2·S7 근거 |
| `helm upgrade --dry-run=client` 라이브 릴리스 대상(차트 사본에 신규 기본 키 추가) | `--reuse-values` → 신규 키 **0회** 렌더, `--reset-then-reuse-values` → 1회 (M5 근거) |

---

## 라운드 1 — NOT CONVERGED (M5 + S10 + N15)

### Must fix (M)

**M1. 장시간 SSE가 매 롤아웃·스케일인마다 SIGKILL — 그 스트림의 사용량과 lifespan drain이 사라짐**
(`values.yaml:475` `terminationGracePeriodSeconds: 60`, `templates/gateway-proxy/deployment.yaml:91` preStop `sleep 15`,
`gateway-proxy/Dockerfile:43` uvicorn에 `--timeout-graceful-shutdown` 없음, `gateway-proxy/src/app/main.py:426` lifespan 데드라인 40s)
- 재현: prod `STREAM_IDLE_TIMEOUT=540`. Opus 응답이 2분 걸리는 스트림 도중 HPA 스케일인이나 rollout이 일어남 → preStop 15s → SIGTERM.
  uvicorn은 열린 연결이 끝날 때까지 기다리고(상한 없음), lifespan shutdown(cost spool / body_log drain, 40s 예산)은 그 **뒤에** 돈다.
  스트림이 45s 안에 안 끝나면 60s에 SIGKILL이 온다. finalize도, 부분 비용 기록도, 스풀 drain도 실행되지 않는다.
  결과: 소비한 토큰이 usage/예산에 안 잡힌다(과금 누락). 클라이언트는 깨끗한 SSE 에러 프레임이 아니라 잘린 스트림을 받는다.
  lifespan 예산 산식(15+40<60)은 "SIGTERM 순간 연결이 0개"일 때만 성립한다.
- 최소 수정: `terminationGracePeriodSeconds`를 preStop + 스트림 상한 + 40으로 잡는다(prod ≈ 600, dev ≈ 300. Fargate는 허용).
  CMD에 `--timeout-graceful-shutdown $((grace-15-40-5))`를 넣어, 스트림이 아무리 길어도 SIGKILL 전에 lifespan이 반드시 돌게 한다.
  ECS `stopTimeout`은 120s가 상한이라 ECS tier에서는 이 상한을 문서에 적는다.

**M2. `internal_api_token`은 ExternalSecret에서 무조건 필수인데, 시크릿 계약·문서·install 안내 어디에도 없음 → 신규 환경 전체 기동 실패**
(`templates/common/secret.yaml:86-89` 조건 없이 매핑 / `deployment/docs/secrets-contract.md`에 키 없음 /
`docs/eks-fargate/03-secrets.md:250` "3 key" / `scripts/install-eks.sh:305` 생성 안내에 3키만 있음)
- 재현: 새 계정에서 03 문서나 install-eks.sh 안내대로 `/llm-gateway/<env>/app`에 3키만 만든다.
  ESO는 property 하나라도 없으면 Secret 전체 sync를 실패시킨다(템플릿 주석 L82-85, L90-92가 바로 이 동작을 적어 둠).
  그러면 `llm-gateway-app` Secret이 생기지 않는다. migration initContainer는 DB Secret만 기다리므로 통과하지만,
  VK 암호화 키·NEXTAUTH를 참조하는 gateway-proxy / admin-api / admin-ui가 전부 CreateContainerConfigError가 된다.
  `--atomic`이 롤백하므로 결과는 설치 실패다. 09 문서의 "키가 없으면 엔드포인트만 403"이라는 설명도 사실과 다르다.
- 최소 수정: secrets-contract.md, 03-secrets.md 체크리스트, install-eks.sh:305 안내에 `internal_api_token`(`openssl rand -hex 32`)을 추가한다.
  또는 `githubWebhook`처럼 `adminApi.internalApi.enabled` 조건으로 감싼다.

**M3. 커밋된 values의 이미지 핀이 라이브·db/ head보다 뒤처짐 — 정식 경로 `install-eks.sh`가 hook 실패 또는 전 서비스 다운그레이드**
(`values-eks-fargate-dev.yaml:527`, `values-eks-fargate-prod.yaml:523` `migration.image.tag: 1.0.54-us19` / 서비스 태그 `1.0.87-us19` 등 /
`install-eks.sh:434-450` SET_ARGS에 image.tag 없음)
- 근거: `db/versions` head는 `0048`이고, 라이브 rev 222 migration 태그는 `eed423d6`이다.
  gitignored `values-eks-fargate-dev.local.yaml:547-549`에 이미 실사고 기록이 있다: "1.0.55-head0039는 0045 이전이라 pre-upgrade hook이 Can't locate revision '0045'로 실패".
  1.0.54는 그보다도 오래된 이미지다.
- 재현: `install-eks.sh dev`를 실행한다(TUI install-eks 스텝도 동일).
  migration 1.0.54-us19가 `alembic upgrade head` → DB 현재 리비전(≥0045)을 못 찾아 hook 실패 → `--atomic` 롤백 → 배포 불가.
  hook이 통과하는 환경이라면 6개 서비스가 SHA 태그에서 us19/us18 이미지로 조용히 **다운그레이드**된다.
  README "prod values는 image.tag 미핀"도 이제 사실과 다르다(지금은 핀돼 있지만 낡은 값).
- 최소 수정: 태그의 단일 소스를 정한다. 가장 작은 수정은 install-eks.sh가 `helm get values`에서 현재 서비스별 태그를 읽어,
  `IMAGE_TAG`(또는 서비스별 env) 미지정 시 `--set`으로 그대로 이어 쓰는 것이다.
  더해서 migration 이미지의 head가 `db/versions` head보다 낮으면 중단하는 preflight를 둔다
  (`docker run --entrypoint alembic <img> heads` 또는 태그에 head 표기 강제).

**M4. prod terraform이 cross-account AssumeRole을 IRSA에 배선하지 않음 — prod cowork AccessDenied**
(`terraform/environments/llm-gateway-prod/main.tf` `module "irsa"`에 `cowork_role_arn`·`claude_code_374_role_arn` 없음, prod `variables.tf`에 변수 자체가 없음.
dev `main.tf`는 두 줄을 전달 / `modules/irsa/main.tf`의 `dynamic "statement"`는 빈 문자열이면 렌더 안 됨)
- 재현: migration `0009`가 cowork routing profile에 `account_role_arn=arn:aws:iam::222233334444:role/llm-gateway-cowork-bedrock`을 심는다(env 무관).
  prod gateway-proxy IRSA에 `sts:AssumeRole`이 없으므로 cowork 요청은 전부 AccessDenied다.
  claude-code는 `0022`의 333 role assume이 실패하고 in-account로 투명 폴백한다(동작은 하지만 의도한 계정 분리·과금 분리가 무력화된다).
  이 계정에는 prod role이 없어 라이브 IAM은 확인할 수 없었다(`aws iam list-roles` 결과 prod 0건). 즉 첫 prod apply에서 발현할 잠복 결함이다.
- 최소 수정: dev와 같은 두 변수를 prod `variables.tf`에 선언하고 `module "irsa"`에 전달한다. tfvars.example에도 추가한다.

**M5. 라이브 배포 경로 `rebuild-image.sh`가 `--reuse-values` — 신규 차트 기본 키 유실(BUDGET_PENDINGIDX_WARMUP 사고와 같은 부류)**
(`scripts/rebuild-image.sh:88-90`)
- 근거(실측): 라이브 rev 222를 대상으로, 차트 사본 `values.yaml`에 `gatewayProxy.env.R24_NEW_DEFAULT_KEY` 하나를 추가하고 `helm upgrade --dry-run=client`를 돌렸다.
  `--reuse-values` → 렌더 0회, `--reset-then-reuse-values` → 1회.
  Helm은 reuse 시 새 차트의 `values.yaml` 대신 **이전 릴리스 차트의 coalesced values**를 쓴다. 그래서 새 기본값 키가 통째로 빠진다.
- 재현: values.yaml에 새 env 기본값을 추가하는 커밋 → `DEPLOY=1 rebuild-image.sh gateway-proxy dev` → ConfigMap/env에 그 키가 없다.
  스크립트 주석(L84-87)은 `migration.enabled` 하나만 막고 있다.
- 최소 수정: `--reuse-values` → `--reset-then-reuse-values`(Helm ≥3.14; install-eks.sh가 이미 요구하는 버전). 07 문서 L48도 같이 바꾼다.

### Should fix (S)

**S1. ExternalSecret이 pre-upgrade hook + `creationPolicy: Owner` → 매 upgrade마다 앱 Secret 3개가 삭제 후 재생성됨**
(`templates/common/secret.yaml:57-67,106-116` hook `before-hook-creation` + `creationPolicy: Owner`)
- 근거(실측): `llm-gateway-app/db/redis` Secret의 creationTimestamp가 셋 다 `2026-10-17T11:13:54Z`로, rev 222 upgrade 시각(11:13:52)과 같다. 222번 매번 지웠다 만든 것이다.
- 재현: upgrade 중 helm이 ES를 삭제 → GC가 소유 Secret을 삭제 → 새 ES → ESO 재sync. 이 창 동안 시작하는 pod(HPA 스케일아웃, 크래시 재시작, rollout 신규 pod)는 CreateContainerConfigError가 된다.
  ESO나 SM이 그 순간 불가용이면 창이 무기한으로 늘고, 그동안 재시작하는 pod는 하나도 못 뜬다.
  `install-eks.sh`의 `preinstall_cleanup_es_artifacts`(b)가 다루는 "owner UID 충돌"도 같은 원인에서 나온 증상이다.
- 최소 수정: `creationPolicy: Orphan`(또는 `Merge`). ES 재생성과 무관하게 Secret이 유지되고, ESO가 기존 Secret을 갱신한다. 그러면 install-eks.sh (b) 정리 로직도 지울 수 있다.

**S2. 단일 레플리카 + PDB `minAvailable: 1` → 허용 중단 0, eviction 영구 차단 (라이브)**
(`values-eks-fargate-dev.yaml` admin-api/admin-ui/cost-recorder/notification-worker replicas 1 + base PDB `minAvailable: 1`)
- 근거(실측): `kubectl get pdb` 결과 4개 모두 `ALLOWED DISRUPTIONS 0`.
- 재현: EKS Fargate 보안 패치 eviction이나 `kubectl drain`이 PDB에 막혀 진행되지 않는다. 패치가 밀리거나 강제 종료로 이어진다.
- 최소 수정: dev values에서 해당 4개의 `podDisruptionBudget.enabled: false`를 준다. 또는 PDB 템플릿을 `replicas<2 && !autoscaling`이면 렌더하지 않도록 바꾼다.

**S3. `rebuild-image.sh`의 `[dev|prod]` 인자가 아무 데도 안 쓰임 — 현재 kube context에 배포**
(`rebuild-image.sh:28` `ENV="${2:-dev}"`, 이후 참조 0회. helm/kubectl에 `--kube-context` 없음)
- 재현: 현재 context가 dev인 셸에서 `DEPLOY=1 rebuild-image.sh gateway-proxy prod` → dev에 배포된다. 반대 방향도 같다. ECR도 현재 자격증명 계정을 쓴다.
- 최소 수정: ENV로 cluster 이름을 정하고(`llm-gateway-$ENV`), `kubectl config current-context`가 다르면 중단한다. 또는 `--kube-context`를 명시한다.

**S4. `rebuild-image.sh`의 helm upgrade에 `--timeout`/`--atomic` 없음 — helm 기본 5m < migration 상한**
(`rebuild-image.sh:88-90` vs `values.yaml:384` `activeDeadlineSeconds: 600`, initContainer 대기 300s)
- 재현: 큰 인덱스를 만드는 migration이 5분을 넘긴다 → helm이 hook 대기 timeout으로 릴리스를 FAILED 처리 → Job은 계속 돌아 스키마를 올림 → 앱은 롤아웃되지 않는다.
  values 주석이 경고하는 "새 스키마 + 구 앱" 조합이 생긴다(0031 enum 사례).
- 최소 수정: install-eks.sh와 같이 `--timeout 15m --wait` + `--atomic`(v3) / `--rollback-on-failure`(v4).

**S5. migration 이미지와 db/ 변경을 묶는 게이트가 없음 (실사고 부류). dirty 검사도 migration 경로를 놓침**
(`rebuild-image.sh:49` `git status -- "$SVC"` → SVC=migration이면 존재하지 않는 `migration/`를 검사)
- 재현 1: `db/versions/0049` 추가 + admin-api 코드 변경 커밋 → `rebuild-image.sh admin-api` → migration 태그는 그대로 → 구 이미지 `upgrade head`가 no-op → admin-api가 새 컬럼에서 500.
- 재현 2: 커밋되지 않은 db/ 변경으로 `rebuild-image.sh migration`을 실행해도 경고가 없다. 이미지 내용이 태그(HEAD SHA)와 어긋난다.
- 최소 수정: L49의 경로를 `CTX` 기준(`db`)으로 고친다. admin-api/gateway-proxy 빌드 시 `git diff --quiet <live migration tag> HEAD -- db/`가 실패하면 "migration도 재빌드 필요"로 중단한다.

**S6. tfvars.example(dev/prod)의 Bedrock ARN이 낡음 — 복사하면 문서화된 Claude 5 AccessDenied 사고가 재현됨**
(`environments/llm-gateway-{dev,prod}/terraform.tfvars.example:29-45`: 리전 고정 `ap-northeast-2::foundation-model/anthropic.claude-{opus,sonnet,haiku}-4-*`. Claude 5·Haiku 5·GPT-5.6 0건 / `variables.tf:229-266` 기본값에는 있음)
- 재현: 새 계정이 `cp terraform.tfvars.example terraform.tfvars` → tfvars가 기본값을 덮어씀 → `global.anthropic.claude-opus-5`가 소스 리전 foundation-model ARN 불일치로 AccessDenied.
  variables.tf 주석 L229-230이 적어 둔 바로 그 증상이고, GPT-5.6 runtime plane도 같다.
- 최소 수정: example에서 `bedrock_allowed_model_arns` 블록을 지워 기본값을 쓰게 한다. 또는 variables.tf 기본값과 같게 맞춘다.

**S7. notification-worker SES IRSA role이 IaC 밖에 있음 (라이브는 수동 생성 role)**
(`values-eks-fargate-{dev,prod}.yaml` notificationWorker 주석 "SES 발신용 IRSA role ARN 필요 (terraform output)" / `modules/irsa`에 notification 역할 0건 / ECS 모듈 `iam.tf:242-268`에는 있음)
- 근거(실측): 라이브 SA `notification-worker`가 `llm-gateway-dev-notification-worker-ses`를 쓰고 `EMAIL_SENDER_TYPE=ses`다. 이 role은 terraform 어디에도 없다.
- 재현: 새 계정에서 문서대로 provider를 `ses`로 바꾼다 → terraform output에 role이 없다 → SA 미주석 → Fargate pod에 자격증명이 없어 발송 전량 실패.
- 최소 수정: `modules/irsa`에 `enable_notification_ses` 조건부 role(ses:SendEmail, identity ARN 한정)과 output을 추가하고, install-eks.sh가 SA 주석을 주입한다. 기존 role은 `terraform import`.

**S8. TUI teardown: `helm uninstall`을 기다리지 않고 바로 `terraform destroy` → ALB/SG 고아 경쟁**
(`tui/steps.py:84-94` `_HELM_UNINSTALL`, `skippable=True`)
- 재현: `helm uninstall`은 Ingress 삭제만 요청하고 바로 반환한다(ALB controller finalizer가 ALB를 비동기로 지움).
  곧바로 `terraform destroy`가 alb-controller helm_release와 노드/클러스터를 내린다 → ALB/SG/ENI가 남는다 → VPC 삭제가 DependencyViolation으로 실패한다. 이 스텝이 막으려던 함정 그대로다.
  update-kubeconfig 실패도 skippable이라 destroy는 그대로 진행된다.
- 최소 수정: `helm uninstall --wait --timeout 10m` 뒤에 `kubectl wait --for=delete ingress -n llm-gateway --all --timeout=10m`를 넣는다. 이 단계 실패 시 destroy를 막는다(skippable은 "릴리스 없음" 분기에만).

**S9. Compose tier에 `ALLOWED_AWS_ACCOUNTS`/`ALLOWED_IAM_ROLES`가 없음 → CLI(STS) VK 발급이 항상 403**
(`docs/NDS/deploy/render/compose.py:180-213` `desired_env` / `admin-api/src/app/services/cli_service.py:114` 빈 allowlist는 fail-closed)
- 재현: compose 배포에서 `gateway-cli`의 STS 경로 로그인 → "AWS account allowlist is empty — CLI login is disabled".
  EKS(install-eks `--set`)와 ECS(`taskdefs.tf:29` caller account 폴백)는 채운다. 3 tier 중 compose만 이 기능이 죽어 있다.
- 최소 수정: `desired_env`에 `ALLOWED_AWS_ACCOUNTS`(gateway.yaml 또는 `sts get-caller-identity`)와 `ALLOWED_IAM_ROLES`(cfg 값)를 추가한다. 비어 있으면 render 노트로 경고한다.

**S10. 커밋된 prod values: internet-facing ALB 3개가 HTTP 평문(admin-api 포함), `SECURE_COOKIES=false`**
(`values-eks-fargate-prod.yaml:114-142` listen-ports `HTTP:80`, tls 전부 false. 렌더 결과 `SECURE_COOKIES: "false"`)
- 재현: values 그대로 prod를 배포하면 VK·OIDC id_token·관리 API 호출·세션 쿠키가 인터넷을 평문으로 지난다. install-eks.sh는 이 설정을 바꾸지 않는다.
  (라이브 dev는 local values로 HTTPS 운용 중이라 이 결함은 prod values 파일에 한정된다.)
- 최소 수정: prod values의 기본을 HTTPS(방식 B, `certificate-arn` CHANGE_ME + ssl-redirect)로 둔다. 최소한 admin-api/admin-ui ingress는 `scheme: internal` 또는 `inbound-cidrs`로 제한한다.

### Nice to have (N)

| ID | 발견 | 위치 | 최소 수정 |
|---|---|---|---|
| N1 | migration `wait-for-secret` initContainer에 securityContext 없음(pod 수준만 적용: privesc·capabilities 미차단). 렌더 결과로 확인 | `templates/jobs/migration.yaml:38-61` | 메인 컨테이너와 같은 `containerSecurityContext` include |
| N2 | 전 컨테이너 `readOnlyRootFilesystem: false`. IRSA를 안 쓰는 admin-ui/scheduler/cost-recorder SA도 `automountServiceAccountToken: true` | `values.yaml:56`, 각 `serviceaccount.yaml` | Python 서비스는 `/tmp` emptyDir + RO root, 비-IRSA SA는 automount false |
| N3 | NetworkPolicy 3종이 렌더되지만 EKS Fargate는 NetworkPolicy를 집행하지 않음 → 문서·차트가 주는 격리 인상이 실제와 다름 | `templates/common/networkpolicy.yaml`, `values.yaml:46` | NOTES/문서에 "Fargate 비집행" 명시. 격리는 SG for Pods로 |
| N4 | values만으로는 `OIDC_AUDIENCE=""`(aud 검증 꺼짐, `admin-api/main.py:134-143`)와 `ALLOWED_AWS_ACCOUNTS=""`가 됨. install-eks `--set`에만 의존하므로 `helm upgrade -f values`(reuse 없이) 직접 실행 시 보안 설정이 조용히 풀림 | prod/dev values, `install-eks.sh:441-450` | values에 CHANGE_ME로 명시 + 빈 값이면 `fail` 템플릿 가드(OIDC enabled일 때) |
| N5 | prod `.terraform.lock.hcl`이 OpenTofu로 생성됨(`registry.opentofu.org`, 구 패치 버전). dev/ecs는 terraform. `init -lockfile=readonly` 실패를 확인함. 그 state가 tofu로 쓰였다면 terraform CLI 전환 시 `state replace-provider`가 필요 | `environments/llm-gateway-prod/.terraform.lock.hcl:1` | 사용 도구를 하나로 정하고 lock 재생성 |
| N6 | `terraform fmt -check` 실패 3파일 | `modules/agentcore-runtime/lambdas.tf`, `modules/ecs-gateway/{main,taskdefs}.tf` | `terraform fmt` |
| N7 | 리전·권한 기본값 하드코딩: vpc `aws_region` default `ap-northeast-2`(`modules/vpc/variables.tf:60`), ECS `allowed_sts_regions`/`mantle_regions` default(`modules/ecs-gateway/variables.tf:333,358`. env가 덮어써 현재는 잠복), `bootstrap-tfstate.sh:16` 기본 리전, dev/prod `backend.tf:14` region 고정, ECS `bedrock_allowed_model_arns` default `["*"]`(`variables.tf:351`), 전 모듈 `arn:aws:` 리터럴 | 좌동 | default 제거(필수화) / `-backend-config` region / `data.aws_partition` |
| N8 | 종료 예산 여유: notification-worker `stop_all(18)+drain(10)=28s`에 grace 30이라, redis close·db dispose·OTel flush에 2s만 남음(손실은 텔레메트리 한정). cost-recorder는 Service가 없는데 preStop `sleep 10`으로 종료를 늦출 뿐. ECS cost-recorder `stopTimeout 30` = 코드 grace 30 + 정리 | `notification-worker/src/worker/main.py:257-275`, `templates/cost-recorder-worker/deployment.yaml:93`, `taskdefs.tf:312` | notification grace 40, cost-recorder preStop 제거, ECS 워커 stopTimeout 45 |
| N9 | 문서 불일치: README "prod values는 image.tag 미핀"(지금은 핀돼 있지만 낡음, M3), `values.yaml:369-1020` "= 라이브(dev·prod)" 주석 전부 사실과 다름, 07 문서가 `--reuse-values`를 권장하면서 차트 기본값 유실과 `migration.enabled=false` 잔존은 경고하지 않음, 02 문서 IRSA role 3개 vs 체크리스트 4개 및 tfstate 버킷명(`vanilla` 누락), 09 문서 "키 없으면 403"(M2) | 좌동 | 해당 문장 정정 |
| N10 | `smoke-test.sh --env`는 값을 버림(L39). `--env prod`여도 현재 context를 검사한다. install-eks.sh `main`의 `helm status`가 `setup_kubeconfig`보다 먼저 이전 context로 돈다(표시만 틀림). `patch_nextauth_url`의 2차 upgrade에는 `--atomic`이 없다 | `smoke-test.sh:39`, `install-eks.sh:668-676,575-582` | `--env`로 context를 검증하거나 플래그 삭제 / 순서 교체 / atomic 추가 |
| N11 | `DEPLOY=1 rebuild-image.sh migration`은 upgrade가 성공한 뒤 `rollout status deploy/llm-gateway-migration`(존재하지 않음)에서 set -e로 종료 → 성공을 실패로 보고 | `rebuild-image.sh:95` | SVC=migration이면 rollout status 생략(Job 완료는 helm이 이미 대기) |
| N12 | TUI Tool-Gateway 배포·삭제 메뉴가 존재하지 않는 자산(`environments/tool-gateway-dev`, `scripts/provision_tool_gateway.sh`)을 참조함. preflight가 막지만 tui/README는 기능으로 안내 | `tui/paths.py:10`, `tui/steps.py:66-80` | 메뉴·README에서 제거하거나 자산 복원 |
| N13 | `install-eks.sh`가 실행할 때마다 ESO validating webhook을 **삭제**함(install 한정이 아님) → 다음 cert-controller 재생성 전까지 ES/SecretStore 스키마 검증이 꺼짐 | `install-eks.sh:241-248` | `HELM_ACTION=install`일 때만, 또는 S1 수정 후 삭제 |
| N14 | 차트 테스트가 2개 시나리오뿐. 이번 발견 중 M2(ES 키 ↔ 계약), M3(migration head ↔ db/), S2(PDB ↔ replicas), M1(grace ≥ preStop+drain)은 렌더 기반 테스트로 잡을 수 있음 | `charts/llm-gateway/tests/` | 렌더 테스트 3~4개 추가 |
| N15 | capture 백업 `gateway.captured-*.yaml.bak`이 `write_text`로 0664 생성(원본은 0600) | `docs/NDS/deploy/cli.py:871-872` | 백업도 0600으로 기록 |

### 리뷰어 제기 → 검증 후 기각/강등

- "ALB deregistration delay 300s 동안 새 연결이 종료 pod로 감": 틀림. draining target에는 새 요청이 가지 않는다. 실제 문제는 기존 스트림 절단이고, M1로 흡수했다.
- "ECS gateway-proxy에 BUDGET_PENDINGIDX_WARMUP 없음": 앱 기본값이 `True`(`gateway-proxy/src/app/config.py:148`)라 동작 차이가 없다. 기각.
- "ECS allowed_sts_regions 기본값 때문에 CLI 거부": env `gateway-ecs/main.tf:185`가 `[var.aws_region]`으로 폴백한다. 잠복 N7으로 강등.
- "values에 allowedAwsAccounts가 비어 CLI 장애(M)": 정식 경로 install-eks.sh는 주입한다. 수동 경로의 보안 퇴행으로 N4에 묶었다.
- `enable-bedrock-vpce.sh`의 `set -uo pipefail`(-e 없음): 의도적이다. grep/aws의 "못 찾음"을 분기로 쓰고 실패 경로마다 `|| die`가 있다. 발견 아님.

### 확인 결과 문제 없음 (검증함)

- install-eks.sh: `--reuse-values`를 쓰지 않음(`-f values + --set`), `--timeout 15m` > hook 600s, v3/v4 rollback 플래그 분기, `--cleanup-on-fail`, 리전 가드, STS 리전 합집합(herestring으로 SIGPIPE 회피).
- migration hook 순서: ES -30 → SA/Role/RoleBinding -10 → Job -5. 최초 install에서도 SA가 hook이라 먼저 존재한다. Job 이름에 revision 포함, `hook-succeeded` 삭제 + 실패 Job은 ttl 600s 동안 보존.
- 시크릿 평문 없음: 전부 `secretKeyRef`(ConfigMap·`value:`에 시크릿 0건). ES target 이름과 secretKeyRef 이름 일치.
- probes: gateway/admin-api readiness·startup은 `/health/ready`, liveness는 `/health`. 워커는 exec probe.
- prod HPA/PDB: min 3/PDB 2, min 2/PDB 1 등 항상 ≥1 evictable. 모든 RollingUpdate가 `maxUnavailable: 0`. scheduler는 `replicas: 1` 하드코딩 + `Recreate` + PDB off(중복 cron 없음).
- `checksum/config`·`checksum/secret` pod 주석으로 설정 변경 시 자동 롤아웃. podSecurityContext(runAsNonRoot, uid 1000, seccomp RuntimeDefault)는 migration 포함 전 pod에 적용. `imagePullPolicy: Always`/`latest` 0건. kubectl 이미지는 digest 고정.
- `STREAM_IDLE_TIMEOUT` < ALB idle: dev 240 < 300, prod 540 < 600(렌더 결과).
- terraform: Aurora `storage_encrypted`, prod `deletion_protection`·final snapshot·백업 14일, Valkey at-rest/in-transit 암호화, 데이터스토어 SG가 private CIDR 한정, state 버킷 versioning·SSE·public access block, IRSA trust의 `llm-gateway:gateway-proxy|admin-api`가 차트 SA명과 일치, AssumeRole이 특정 ARN 한정, SM 접근이 `/${project}/${env}/*` 한정, body-log 쓰기 전용, AgentCore us-east-1 고정은 정당(관리형 커넥터 제약), invocation-logging 리전 가드.
- ECS: deployment circuit breaker + rollback, `terraform_data.migration`이 exit 0을 확인한 뒤 서비스 생성, `STREAM_IDLE_TIMEOUT` 주입, `ALLOWED_AWS_ACCOUNTS` caller 폴백.
- 스크립트/TUI: rebuild-image의 동일 태그 덮어쓰기 차단, admin-api↔scheduler 태그 동기화, migration 빌드 컨텍스트 `db/`. smoke-test는 VK를 출력하지 않고 prod에서 issue-key를 차단. bootstrap-tfstate는 멱등(403/404 구분). TUI는 `shell=True` 0건이고 plan 파일로 apply(재계산 승인 없음), teardown은 토큰을 정확히 타이핑해야 진행, 키 파일 0600. Compose `.env` 0600, migration `service_completed_successfully` 의존, `restart: unless-stopped`.

### 범위 밖 관찰 (비차단)

- gitignored `values-eks-fargate-dev.local.yaml:132`의 `inbound-cidrs: 65.2.11.169/32,0.0.0.0/0`이 라이브 dev ALB 3개에 적용돼 있다. `/32` 허용 목록이 `0.0.0.0/0` 때문에 무의미하다. 의도된 개방이 아니라면 운영자 확인이 필요하다.
- 라이브 dev EKS public endpoint `0.0.0.0/0`은 코드 주석에서 이미 인지하고 있는 상태다.

Verdict: M1~M5와 S1~S10을 먼저 고쳐야 한다. M3·M5·S3~S5는 같은 근본 원인에서 나온다: 배포 경로가 둘(install-eks vs rebuild-image)로 갈라져 있고, 이미지 태그와 values의 단일 소스가 없다. 한 번에 정리하는 것이 가장 작은 수정이다.
Not checked: prod 클러스터·prod IAM 실물(이 계정에 없음), 실제 Fargate eviction 동작, ESO 다운 상태에서의 upgrade(S1의 장기화 시나리오는 메커니즘으로만 판정).

VERDICT: NOT CONVERGED

---

## 라운드 1 — 처리 내역 (2026-10-17, 이후 kiro 라운드 2 검증 대상)

### M — 전부 수정 완료

| ID | 수정 |
|---|---|
| M1 | `Dockerfile`: `--timeout-graceful-shutdown ${UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:-240}` 추가. `values.yaml`: grace 60→300 + `UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:"240"` env, 산식 주석(15 preStop+graceful+40 lifespan+5). prod values: grace 630 + env 570. |
| M2 | `secrets-contract.md`(표+예시 3곳), `03-secrets.md`(키 생성·표·JSON 3곳·체크리스트 3→4 key), `install-eks.sh` 안내에 `internal_api_token` 추가. |
| M3 | `install-eks.sh` helm_install: upgrade 시 `helm get values`로 서비스별 라이브 태그를 읽어 `--set` 유지(`IMAGE_TAG` env로 일괄 오버라이드). migration 게이트: 대상 태그가 git commit이고 그 이후 `db/` 변경 있으면 중단. |
| M4 | prod `variables.tf`에 `cowork_role_arn`/`claude_code_374_role_arn` 추가(dev와 동일 기본값), `main.tf`의 `module.irsa`에 전달, prod tfvars.example에 주석 블록. |
| M5 | `rebuild-image.sh`: `--reuse-values` → `--reset-then-reuse-values` (helm ≥3.14). 문서 2곳(05·07) 동일 갱신. |

### S — 전부 수정 완료

| ID | 수정 |
|---|---|
| S1 | `secret.yaml` 3개 ES `creationPolicy: Owner`→`Orphan` + 주석. install-eks.sh cleanup (b): 삭제 → stale ownerReference patch 제거로 전환(Orphan 전환 첫 upgrade의 GC 삭제 창 제거). |
| S2 | pdb.yaml 5개에 `(replicaCount>1) or (autoscaling.enabled && minReplicas>1)` 렌더 가드 — 정적 1레플리카 PDB(허용 중단 0) 제거. |
| S3 | `rebuild-image.sh`: DEPLOY=1 시 `kubectl config current-context` == `llm-gateway-$ENV`(`KUBE_CONTEXT`로 재지정 가능) 검사, 불일치 시 중단. |
| S4 | `rebuild-image.sh` helm upgrade에 `--timeout 15m --wait --atomic` 추가. |
| S5 | dirty 검사를 `$CTX` 기준으로 수정(migration→db/). DEPLOY=1 시 `git diff <live migration tag> HEAD -- db/` 게이트 추가. |
| S6 | dev/prod tfvars.example의 낡은 `bedrock_allowed_model_arns` 블록 제거 → variables.tf 기본값 사용 + 경고 주석. |
| S7 | `modules/irsa`: `enable_notification_ses` 조건부 SES role + output `notification_worker_ses_role_arn`. env 2곳 variables/main/outputs 배선. install-eks.sh가 output 존재 시 SA 어노테이션 주입 + provider=ses인데 role 없으면 경고. |
| S8 | `tui/steps.py` `_HELM_UNINSTALL`: `helm uninstall --wait --timeout 10m` + `kubectl wait --for=delete ingress`. skippable 제거("릴리스 없음"은 스크립트가 exit 0). |
| S9 | `compose.py` desired_env에 `ALLOWED_AWS_ACCOUNTS`(aws.account_id)/`ALLOWED_IAM_ROLES`(aws.allowed_roles) 주입 + schema에 `aws.allowed_roles` 추가 + feature_notes 경고. |
| S10 | prod values ingress: HTTPS 리스너 + ssl-redirect + certificate-arn CHANGE_ME 기본. `SECURE_COOKIES`를 tls.enabled OR HTTPS 리스너 어노테이션으로 파생(테스트로 검증). |

### N — 처리한 항목

- N1: migration initContainer에 `llm-gateway.containerSecurityContext` include.
- N2: admin-ui/scheduler/cost-recorder SA `automountServiceAccountToken: false`. notification-worker는 IRSA 어노테이션 유무로 조건부.
- N3: NOTES.txt에 "Fargate는 NetworkPolicy 미집행 — SG for Pods 사용" 안내.
- N4: NOTES.txt에 `adminApi.oidc.audience` 빈 값 경고(aud 검증 꺼짐).
- N6: `terraform fmt` 3파일 + env 2곳 정리.
- N8: notification grace 30→40, cost-recorder preStop 제거(Service 없음), ECS 워커 stopTimeout 30→45.
- N9: 07 문서 `--reset-then-reuse-values`+경고, 05 문서 동일, README의 "prod 태그 미핀" 문장을 라이브-태그 보존 방식으로 정정.
- N10: smoke-test `--env` → `llm-gateway-$ENV` context 매핑. install-eks.sh: setup_kubeconfig를 helm status 앞으로 이동. patch_nextauth_url 2차 upgrade에 ROLLBACK_FLAG+cleanup-on-fail.
- N11: rebuild-image.sh에서 SVC=migration이면 rollout status 생략.
- N12: TUI 메뉴에서 tool-gateway 자산 미존재 시 관련 항목 숨김(`_tool_assets_present`).
- N14: `tests/test_deployment_safety.py` 신규 — M1 산식/S1 Orphan/M2 키 매핑/S2 PDB 가드/S10 SECURE_COOKIES 렌더 검증(총 8 테스트).
- N15: capture .bak 파일 `chmod 0600`.

### 미처리 (의도적 보류)

- N5: prod `.terraform.lock.hcl`이 OpenTofu 생성 — 도구 통일 결정은 사용자 영역. state가 tofu로 쓰였다면 terraform 전환 시 `state replace-provider` 필요(관찰 기록 유지).
- N7: 리전 기본값 하드코딩 — 현재 env가 모두 덮어써 잠복. 모듈 API 변경은 영향이 커서 별도 라운드 권장.
- N13의 잔여: webhook 삭제는 install 시에만 하도록 게이트(처리 완료).

### 검증

- `helm lint` + `helm template` 4개 values 조합 전부 성공.
- 차트 테스트: 37 passed (신규 8 포함).
- TUI 테스트: 69 passed, 1 skipped (teardown 스텝 계약 테스트 2개 갱신/추가).
- docs/NDS/deploy 테스트: 109 passed (live local values SHA 태그 drift 로 깨져 있던 capture 테스트 1건을 태그 독립적으로 정정).
- `terraform validate` (dev/prod/gateway-ecs) 3개 통과, `terraform fmt -check` 전부 통과.
- `bash -n` 전 스크립트 통과.

---

## 라운드 2 — 검증 결과: NOT CONVERGED (S5 + N14, 리뷰어가 전부 실행 기반 판정)

검증 파일: `tasks/review-round-ponytail-opus-r24-deployment-2026-10-round2-result.md`

통과 확인: M1(EKS 산식)/M2/M4(terraform plan 실측)/M5/S1/S3~S9 전부 의도대로 동작. 자동마운트=off 안전성 코드 근거로 확인. 잔여: M3 첫-install IMAGE_TAG 무시 + 게이트 기준 오류, S10 NEXTAUTH_URL/per-ingress override, M1의 prod ALB(300s 절단)·ECS(command 상한 없음) 잔여 + N14건.

### 라운드 2 — 처리 내역

| ID | 처리 |
|---|---|
| S-1 | install-eks: IMAGE_TAG 루프를 upgrade 블록 밖으로 이동(첫 install 적용). upgrade 시 `helm get values` 실패 → `{}` 폴백 대신 `exit 1`. |
| S-2 | migration 게이트 비교 기준을 HEAD→배포될 앱 태그로 수정(`git diff mig_tag app_tag -- db/`, 둘 다 SHA일 때만). 롤백/values-only upgrade 오탐 해소. rebuild-image 측 게이트는 그대로(그쪽은 HEAD 비교가 정탐). |
| S-3 | `_helpers.tpl`에 `llm-gateway.adminUiHttps` 추가 — ingress.yaml:91과 동일 merge(adminUi>defaults>공통). SECURE_COOKIES+NEXTAUTH_URL scheme 양쪽이 이 헬퍼를 씀. `patch_nextauth_url`은 차트 렌더 scheme을 따라 http/https 결정. prod host 3개를 `*.CHANGE_ME.example.com`으로. |
| S-4 | prod `ingress.gateway.annotations`에 `target-group-attributes: deregistration_delay.timeout_seconds=600`(≥15+570). 테스트 산식 추가. |
| S-5 | ECS gateway command에 `--timeout-graceful-shutdown $${UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:-15}`(60=15+40+5). |
| N-1 | PDB 가드: autoscaling.enabled면 `minReplicas>minAvailable`(기본1), 아니면 `replicaCount>minAvailable` — replicaCount=3+HPA min=1 조합의 허용중단0 차단 + 테스트. |
| N-2 | `live_values='{}'` 초기화로 이동, `${live_values:-{}}` 제거. |
| N-3 | upgrade 시 라이브 values에 키 없으면 warn(부분 다운그레이드 경고). |
| N-4 | IMAGE_TAG 지정 시 7개 ECR 리포지토리 `describe-images` preflight — ImageNotFound/RepositoryNotFound면 중단, 그 외 에러는 경고. |
| N-5 | deployer-guide 시크릿 표에 internal_api_token 추가, troubleshooting "3개 key"→"4개 key"+예시 갱신, 09 문서 403→ES sync 실패로 정정, secrets-contract 소비자 admin-api로 정정. |
| N-6 | rebuild-image DEPLOY=1 경로 앞에 stale ownerRef 제거 루프 추가(ExternalSecret ownerRef만). |
| N-7 | irsa 변수 설명 `scheduler.email.provider`→`notificationWorker.email.provider` 정정. dev/prod tfvars.example에 enable_notification_ses 플래그 + terraform import 안내 추가. |
| N-8 | teardown: state 존재 확인 선행(미init 디렉터리 오인 방지) + `kubectl get ns` 도달성 게이트 + helm status 실패를 "not found" stderr로 구분 — 도달 불가면 destroy 중단(fail-open 제거). |
| N-9 | schema.py allowed_roles 스칼라 입력을 리스트로 감쌈 + 테스트 2건. |
| N-10 | rebuild-image DEPLOY=0에서 migration은 Job이므로 rollout status 대신 `get job` 안내. tui/README에 자산 없으면 메뉴 숨김 명시. values.yaml `= 라이브` 주석 7곳→"빌드 시점 스냅샷" 정정. README CHANGE_ME dev10/prod16 정정. |
| N-11 | patch_nextauth_url이 DEBUG_MODE에서 rollback/cleanup 플래그 생략. |
| N-12 | **결정 기록**: prod lockfile은 terraform 포맷 유지 — dev/ecs env가 이미 `terraform init` 생성이라 terraform이 도구 계약. OpenTofu 포맷이었던 것이 오히려 드리프트였음. |
| N-13 | rebuild-image에 Helm ≥3.14 preflight(미달 시 push 전 중단) + helm 실패 시 수동 HELM_CMD 출력. |
| N-14 | 테스트 보강: prod-loadtest를 grace 파라미터에 추가, lifespan 예산을 main.py `_shutdown_deadline`에서 파싱, HPA-min=1 PDB 케이스, per-ingress HTTP override→SECURE_COOKIES=false, NEXTAUTH_URL https, prod deregistration≥15+graceful 산식. |
| 범위외 | compose renderer에 `gateway-proxy.stop_grace_period=300s` 추가(docker 기본 10s SIGKILL이 drain을 자르던 공백 — tier 평형). |

### 라운드 2 후 검증

- helm lint + template(base/dev/prod/prod-loadtest): 전부 성공. prod 렌더: NEXTAUTH_URL=`https://admin.CHANGE_ME.example.com`, SECURE_COOKIES=true, deregistration_delay=600
- `values_tag` awk 함수 실측: migration/gateway/adminUi 태그 정확 추출
- chart tests 15(신규 6건), TUI 69+1skip, NDS 111 passed
- terraform validate 3env + fmt clean
- bash -n install-eks.sh/rebuild-image.sh OK

---

## 라운드 3 — 검증 결과: NOT CONVERGED (S2 + N5)

검증 파일: `tasks/review-round-ponytail-opus-r24-deployment-2026-10-round3-result.md` (리뷰어가 bash 3.2 컨테이너·스텁 하네스·shellcheck로 실측)

### 라운드 3 — 처리 내역

| ID | 처리 |
|---|---|
| S-6 | `declare -A ECR_REPO`(bash 3.2 미지원, macOS 기본 셸에서 즉사) → `ecr_repo()` case 함수로 교체 |
| S-7 | 게이트에 `merge-base --is-ancestor app_tag mig_tag` 조건 추가 — migration 이미지가 앞선 정상 중간 상태(rebuild-image migration 직후) 오탐 해소. 방향 역전(앱이 mig보다 앞 + db/ 변경)은 여전히 차단(실측으로 확인) |
| N-8 잔여 | teardown helm 스텝에 `terraform init -reconfigure $TF_BACKEND_ARGS` 선행 — 미초기화 디렉터리에서 output 실패→"클러스터 없음" 오인 해소. Step env에 backend_args 주입 |
| N-15 | helm 버전 파싱 비숫자 출력 시 명시 중단(조용한 통과 제거) |
| N-16 | PDB 테스트 강화: `endswith("-gateway-proxy")` 정확 매칭(기존 "gateway" in n은 전 이름에 매치돼 무력), HPA-min 가드를 3개 서비스로 파라미터화 |
| N-17 | README의 개수 표현 → "CHANGE_ME 마커를 전부 검색"으로 정정 |

### 라운드 3 후 검증

- chart tests + TUI: 113 passed, 1 skipped
- bash -n: OK / helm lint·template 4값 전부 성공
- merge-base 방향 실측: mig>c75·app=9e00(구) → ancestor=true(통과), 역방향+db/변경 → 차단 유지

---

## 라운드 4 — 검증 결과: NOT CONVERGED (S-6r4 + N5)

검증 파일: `tasks/review-round-ponytail-opus-r24-deployment-2026-10-round4-result.md`

라운드 3 처리분 대부분 실행으로 확인(S-6 install-eks, S-7 방향성, N-15, N-16 3서비스, N-17). 수렴을 막은 유일 항목: `rebuild-image.sh:37`의 `declare -A VALUES_KEY` — S-6과 동일한 bash 3.2 결함이 배포 경로에 잔존.

### 라운드 4 — 처리 내역

| ID | 처리 |
|---|---|
| S-6r4 | rebuild-image.sh `declare -A VALUES_KEY` → `values_key()` case 함수로 교체. 두 스크립트 모두 `bash:3.2` 컨테이너 `bash -n` 통과 |
| S-7r4 | 게이트를 three-dot(`git diff mig...app -- db/`)으로 단순화 — merge-base 조건 제거, 분기 상태에서 mig 쪽 변경만 있을 때 과차단 해소. rebuild-image.sh 게이트도 `live_mtag...HEAD`로 동일 적용 |
| N-8r4 | teardown init 실패를 fail-closed로(`terraform init` 실패 → exit 1, output 없음만 스킵). Step env `TF_BACKEND_ARGS` 주입 + 테스트 추가 |
| N-16r4 | HPA-min 음성 테스트를 5개 서비스 전체로 파라미터화(pdb.enabled=true 명시), 양성 대조(min=2>minAvailable → 렌더) 추가 |
| SW-1 | `ecr_repo`에 `*) return 1` 기본 분기 — 미등록 키면 set -e로 즉시 중단(fail-open 제거) |
| SW-2 | notification-worker automount를 `hasKey`→`ne (index ...) ""` — `role-arn: ""` 빈 값 오탐 방지 |

### 라운드 4 후 검증

- chart tests + TUI: 117 passed, 1 skipped (teardown backend-args 테스트 포함)
- bash -n 두 스크립트 + docker bash:3.2 구문 검증 통과
- helm template prod 렌더 성공
