# R24 — deployment 표면 리뷰 라운드 2 (수정 검증, kiro/opus)

대상: 라운드 1 처리 내역(M5+S10+N15) + `/tmp/r24-round1-full.diff`. 주장은 믿지 않고 실행 결과로만 판정했다.

## 실행한 검증

| 항목 | 결과 |
|---|---|
| `helm template` base / dev / prod / prod+loadtest | 4개 모두 렌더 성공. gateway grace/env: base·dev `300 / 240`, prod·loadtest `630 / 570` → `15+240+40+5=300`, `15+570+40+5=630`. 4개 모두 등식 성립 |
| Dockerfile CMD (`sh -c`, dash) | `${UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:-240}` 확장 실측: 미설정 → 240, 570 → 570, 빈 문자열 → 240. `--workers` 중첩 기본값도 정상 |
| uvicorn 0.44 `Server.shutdown` 소스 | graceful 타임아웃이 지나면 task를 cancel한 뒤 `lifespan.shutdown()` 실행. 취소된 스트림은 `streaming.py:415`에서 `_drain_remaining` 정산 태스크를 띄우고, lifespan이 `drain_pending_tasks`로 그 태스크를 기다림 → M1의 과금 보존 경로 성립 |
| 라이브 dev `helm upgrade --reset-then-reuse-values --dry-run=client` (읽기 전용) | 새 기본값 흡수 확인(grace 300, UGST env 240, notification grace 40). ES 3개 Orphan, PDB 0개, notification SA는 automount=true(SES 어노테이션 있음), scheduler/cost-recorder/admin-ui SA는 false |
| `terraform init -backend=false -lockfile=readonly` + `validate` (dev / prod / gateway-ecs) | 3개 모두 valid. prod는 lock 재생성 덕에 readonly init도 통과(N-12 참조). `fmt -check -recursive` 통과 |
| irsa 모듈 실제 `terraform plan` (스크래치 루트, 읽기 전용, ses×cowork 4조합) | cowork 설정 시 AssumeRole 2개, 빈 값이면 claude-code 1개. `ses=false`는 SES 리소스 0 + output `""`, `ses=true`는 policy+role 생성 |
| install-eks.sh 태그/게이트 블록 하네스 (helm stub, 실제 git) | T1~T7 결과는 아래 S-1·S-2·N-2·N-3에 기록 |
| rebuild-image.sh 전체 실행 (aws/docker/helm/kubectl stub, 실제 git) | context 불일치는 빌드 전에 중단. 게이트: 구 SHA → 중단, 최신 SHA → 통과, `1.0.x` → 스킵. SVC=migration은 게이트·rollout 생략. dirty 검사는 `db/`를 감지 |
| ESO v0.10.4 소스 (라이브 이미지 버전) | Orphan 모드는 기존 ownerReferences를 지우지 않고, Secret 삭제 이벤트를 watch해 재생성함 |
| 라이브 TG 속성 (읽기 전용) | gateway-proxy / admin-api / admin-ui 모두 `deregistration_delay=300` |
| pytest | chart 37 passed(신규 10건 = 파라미터 포함), TUI 101 passed, docs/NDS 109 passed |
| 신규 테스트 변이 검사 (차트 사본) | grace↓·base grace 60·UGST<idle·Owner 복귀·internal token 제거·PDB 가드 제거 ×2·SECURE_COOKIES 복귀·preStop 30 → 모두 FAIL. loadtest overlay grace 60, HPA min=1 → **PASS** (N-14) |
| `bash -n` 전 스크립트 | OK. shellcheck는 이 호스트에 없어 미실행 |

## 항목별 판정

| ID | 판정 | 근거 |
|---|---|---|
| M1 | ✅ EKS 산식은 성립. ⚠️ prod·ECS에 잔여 | S-4(ALB 300s 절단), S-5(ECS command에 상한 없음) |
| M2 | ✅ 생성 경로 3곳 + ES 템플릿 일치 | 진단 문서 잔여는 N-5 |
| M3 | ⚠️ upgrade 보존은 동작함 | 첫 install의 IMAGE_TAG 무시(S-1), 게이트 비교 기준 오류(S-2) |
| M4 | ✅ | validate + plan으로 AssumeRole statement 확인 |
| M5 | ✅ | 라이브 dry-run에서 새 기본 키 흡수, `--timeout 15m --wait --atomic` 확인. Helm ≥3.14 요구와 일치 |
| S1 | ✅ install-eks 경로 | 라이브 배포 경로(rebuild-image)에 전환 창이 1회 남음(N-6) |
| S2 | ⚠️ 정적 1레플리카는 OK | "autoscaling minReplicas<=1 포함" 주장은 틀림(N-1) |
| S3 / S4 / S5 | ✅ | stub 실행으로 조합 전부 확인. DEPLOY=0 안내문 잔여는 N-10 |
| S6 | ✅ | example에 활성 `bedrock_allowed_model_arns` 0줄. variables.tf 기본값에 Claude 5 / GPT-5.6 포함 |
| S7 | ✅ 모듈 | count=0/1 plan 확인. 라이브 수동 role과 이름 충돌(N-7) |
| S8 | ✅ 주 시나리오 | 클러스터 접근 실패 시 fail-open(N-8) |
| S9 | ✅ | 렌더 실측. 스칼라 입력 시 문자 단위로 쪼개짐 + 테스트 없음(N-9) |
| S10 | ⚠️ SECURE_COOKIES는 prod 렌더 true | NEXTAUTH_URL / per-ingress override와 불일치(S-3) |
| N1~N4, N6, N8, N13, N15 | ✅ | 렌더/코드로 확인 |
| N9~N12 | 부분 처리 | N-5, N-10 |
| N14 | ✅ 대부분 계약을 잡음 | 공백은 N-14 |
| automount=false (scheduler / cost-recorder / admin-ui) | ✅ 안전 | 세 서비스 코드에 k8s client·SA 토큰 경로·Lease 사용 없음(grep). scheduler의 잡 락은 DB 기반(`_release_job_lock`). IRSA webhook은 automount와 무관하게 자체 projected token을 주입하므로, 어노테이션 조건부인 notification도 안전 |
| install-eks `main()` 순서 변경 | ✅ | `read_tf_outputs`/`setup_kubeconfig`는 `HELM_ACTION`을 참조하지 않음(set -u 안전). `ROLLBACK_FLAG`는 `helm_install`에서 전역으로 설정된 뒤 `patch_nextauth_url`이 실행됨 |

---

## 잔여/신규 발견

### Should fix (S)

**S-1. install-eks.sh: 첫 install에서 `IMAGE_TAG`를 무시하면서 게이트는 `IMAGE_TAG`로 통과시킴. upgrade 시 `helm get values` 실패도 조용히 낡은 핀으로 폴백** (`deployment/scripts/install-eks.sh:487-499, 488, 505`)
- 재현(하네스 T1b): `HELM_ACTION=install IMAGE_TAG=c75d1e92` → `SET_ARGS`에 image.tag가 0개다. 반면 `mig_tag=c75d1e92`라 게이트는 통과한다.
  결과: 신규 계정이 README가 안내한 대로 `IMAGE_TAG=<sha>`를 줘도 values 핀(`migration 1.0.54-us19` 등)이 그대로 배포된다. 낡은 head로 스키마가 만들어지고 앱은 500이 나는 M3 원래 증상이 첫 install에 그대로 남는다.
- 재현(T2): upgrade 중 `helm get values`가 실패하면(API 순간 장애·RBAC) `|| echo '{}'` → 태그 0개 → 전 서비스가 values 핀으로 다운그레이드된다. M3가 막으려던 경로 그대로다.
- 최소 수정: `IMAGE_TAG` 루프를 `if [ "$HELM_ACTION" = upgrade ]` 블록 밖으로 뺀다. upgrade에서 `helm get values` 실패는 `exit 1`로 처리한다. 선택 사항으로, `IMAGE_TAG` 지정 시 7개 repo에 `aws ecr describe-images`로 태그가 있는지 먼저 확인한다(N-4).

**S-2. install-eks.sh migration 게이트의 비교 기준이 HEAD여서, 정식 경로에서는 false positive만 낸다** (`install-eks.sh:505-513`)
- upgrade에서는 앱 태그가 라이브 그대로 유지되거나(IMAGE_TAG 미지정), 전 서비스가 같은 태그로 함께 움직인다(IMAGE_TAG 지정). 어느 쪽이든 "앱은 새 스키마인데 migration은 구 이미지"인 조합이 생기지 않는다. 그런데 게이트는 `mig_tag..HEAD`의 db/ 변경을 본다.
- 재현(T6): `IMAGE_TAG=dd081b50`(전 서비스를 직전 SHA로 일관 롤백) → "db/ 가 … 이후 변경됐다"로 `exit 1`. 이전 SHA로 롤백하는 정상 경로가 막힌다.
  같은 이유로, db/ 커밋이 하나라도 아직 미배포인 동안은 values만 바꾸는 upgrade도 전부 거부된다. 탈출구는 미배포 스키마를 먼저 내보내는 것뿐이다.
  T4는 rebuild-image에서라면 정탐이지만 install-eks에서는 오탐이다.
- 최소 수정: install-eks에서는 "배포될 migration 태그 vs 배포될 adminApi/gatewayProxy 태그"를 비교한다(`git diff --quiet "$mig_tag" "$app_tag" -- db/`, 둘 다 SHA일 때만). 또는 이 게이트를 지우고 rebuild-image.sh에만 둔다(그쪽 게이트는 앱이 HEAD로 가므로 올바름. stub 실행으로 확인함).

**S-3. S10 미완: prod가 HTTPS 기본인데 `NEXTAUTH_URL`은 `http://`로 렌더되고, SECURE_COOKIES 파생은 Ingress별 override를 무시함** (`templates/admin-ui/deployment.yaml:75-76, 118-119`, `values-eks-fargate-prod.yaml:133-150`, `install-eks.sh` `patch_nextauth_url`)
- 재현 1: `helm template -f values-eks-fargate-prod.yaml` → `NEXTAUTH_URL: "http://"`, `SECURE_COOKIES: "true"`.
  NEXTAUTH_URL의 scheme은 여전히 `tls.enabled`에서만 파생된다. 그래서 `patch_nextauth_url`이 `http://<ALB-DNS>`를 넣는다. ssl-redirect와 Secure 쿠키 조합에서 콜백 URL과 쿠키 스킴이 어긋난다.
  게다가 prod `host: ""`(CHANGE_ME 아님)에 ACM 인증서를 붙이면 ALB DNS 이름과 인증서가 맞지 않는다. CLI 클라이언트(gateway-cli·Claude Code)는 TLS 검증에서 실패한다.
- 재현 2: `--set ingress.adminUi.annotations.alb…/listen-ports='[{"HTTP":80}]'` → admin-ui Ingress는 HTTP 전용으로 렌더되는데 `SECURE_COOKIES: "true"`다. 브라우저가 HTTP에서 Secure 쿠키를 버려 로그인 불가.
  원인: 파생식은 공통 `ingress.annotations`만 보는데, `ingress.yaml:91`은 `adminUi.annotations`가 공통값을 덮어쓴다.
- 최소 수정: `ingress.yaml:91`과 같은 merge로 `_helpers.tpl`에 `llm-gateway.adminUiHttps`를 하나 만든다. 이것을 SECURE_COOKIES와 NEXTAUTH_URL scheme 양쪽에 쓰고, `patch_nextauth_url`도 그 값에 따라 `https://`를 쓴다.
  prod values의 `gateway/adminUi/adminApi.host`를 `CHANGE_ME` 도메인으로 바꾼다(README "prod 10개" 마커 수 갱신). 테스트에 per-ingress override 케이스를 추가한다.

**S-4. M1 잔여(prod): ALB deregistration delay 300s < 15 + 570 → 긴 스트림은 롤아웃 300s 시점에 ALB가 끊음** (`values-eks-fargate-prod.yaml:117-130`. 차트 전체에 `deregistration`·`target-group-attributes` 0건)
- 근거(실측): 라이브 TG 3개 모두 `deregistration_delay.timeout_seconds=300`. prod values도 설정하지 않으므로 기본값 300이다.
- 재현: prod에서 스트림 도중 롤아웃 → 15s preStop → uvicorn은 570s까지 기다리지만, ALB가 T+300에 연결을 끊는다. 클라이언트는 SSE 에러 프레임 없이 잘린 스트림을 받는다.
  과금은 cancel → drain 경로로 보존된다. 하지만 "graceful(570) ≥ idle(540)이면 정상 스트림이 조기 절단되지 않는다"는 수정 주석과 `test_gateway_graceful_timeout_not_below_stream_idle`의 계약은 prod에서 실제로 성립하지 않는다(dev는 15+240=255 < 300이라 OK).
- 최소 수정: prod `ingress.gateway.annotations`에 `alb.ingress.kubernetes.io/target-group-attributes: deregistration_delay.timeout_seconds=600`(≥ preStop+graceful)을 추가하고 테스트에 산식을 넣는다. 또는 prod graceful을 ≤285로 낮추고 주석을 정정한다.

**S-5. M1 잔여(ECS tier): ECS gateway command가 CMD를 덮어써 `--timeout-graceful-shutdown`이 없음 → stopTimeout 60s SIGKILL이 lifespan보다 먼저 옴** (`terraform/modules/ecs-gateway/main.tf:43`, `taskdefs.tf:314`)
- 재현: ECS command는 `exec uvicorn … --workers $${WORKERS:-4}`다. 이미지 CMD의 상한이 적용되지 않는다(command override).
  ECS는 deregistration(기본 300s) 뒤에 SIGTERM을 보낸다. 그때까지 남은 스트림이 하나라도 있으면 uvicorn은 무한 대기하고, 60s에 SIGKILL이 온다. 그 결과 cost spool·body_log drain이 실행되지 않는다(라운드 1 M1과 같은 결함).
  라운드 1 요구("ECS 상한 문서화")에 대해 코드 주석 한 줄만 추가됐다.
- 최소 수정: ECS command에 `--timeout-graceful-shutdown $${UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:-15}`를 추가한다(60 = 15 + 40 + 5. deregistration이 SIGTERM 전에 스트림을 이미 비우므로 15s면 충분).

### Nice to have (N)

| ID | 발견 | 위치 | 재현/근거 | 최소 수정 |
|---|---|---|---|---|
| N-1 | PDB 가드의 `or (gt replicaCount 1)`이 HPA 사용 시 틀림. HPA가 켜지면 replicaCount는 무시되는데, 가드는 replicaCount 2만 보고 PDB를 렌더한다. "autoscaling minReplicas<=1 포함" 주장은 사실과 다르다. minAvailable ≥ minReplicas도 검사하지 않는다 | `templates/*/pdb.yaml:3-6` (5개) | `--set adminApi.autoscaling.minReplicas=1` (prod, replicaCount 3) → HPA min 1 + PDB minAvailable 2 렌더 = 허용 중단 0. 커밋된 values는 모두 min≥2라 잠복 | `if autoscaling.enabled then gt minReplicas minAvailable else gt replicaCount minAvailable` + 테스트 |
| N-2 | `"${live_values:-{}}"`가 값이 있을 때 `}`를 하나 덧붙임. jq가 첫 결과를 출력한 뒤 파싱 에러를 내고, `\|\| true`가 그 에러를 삼켜 우연히 동작 | `install-eks.sh:505` | `bash -c 'lv="{\"a\":1}"; echo "${lv:-{}}"'` → `{"a":1}}` | 위에서 `live_values='{}'`로 초기화하고 `<<<"$live_values"` |
| N-3 | 라이브에 일부 서비스 태그만 있으면 나머지는 values 핀으로 폴백(부분 다운그레이드) | `install-eks.sh:489-492` | T3: gatewayProxy·migration만 보존. 현 라이브는 7개 모두 있어 잠복 | upgrade 시 키 누락 → warn 또는 중단 |
| N-4 | `IMAGE_TAG` 일괄 지정은 7개 이미지가 모두 그 태그로 푸시돼 있다고 전제함. rebuild-image는 서비스 하나씩 빌드하므로 대개 성립하지 않고, ImagePullBackOff → atomic 롤백으로 끝남 | `install-eks.sh:493-497`, README:223 | 코드 확인 | ECR 존재 preflight 또는 README 문구 정정 |
| N-5 | M2 문서 잔여: deployer-guide 시크릿 표에 `internal_api_token` 없음. troubleshooting의 "이 3개 key가 일치해야" 예시 출력이 바로 이 장애를 오진하게 만듦. 09 문서 "키가 없으면 403"(ES 전체 sync가 실패하므로 사실이 아님). 계약 표의 소비자에 gateway-proxy가 있으나 실제 소비는 admin-api뿐 | `guides/deployer-guide.md:190-196`, `deployment/docs/eks-fargate/troubleshooting.md:1020-1026`, `09-post-deploy-tui.md:83`, `secrets-contract.md:12` | grep: `INTERNAL_API_TOKEN` 참조는 admin-api 템플릿·코드에만 있음 | 4줄 정정 |
| N-6 | S1 전환 창이 라이브 경로에 1회 남음. ownerRef 제거 patch는 install-eks에만 있는데, 라이브 배포는 rebuild-image.sh다. 머지 후 첫 rebuild-image 배포 때 구 ES 삭제 → GC가 Secret 삭제 → ESO가 재생성하는 창이 한 번 더 생김(ESO 0.10.4는 Orphan에서 기존 ownerRef를 보존하고 delete watch로 재생성하므로 수 초) | `rebuild-image.sh`, `install-eks.sh:263-276` | ESO v0.10.4 소스 + 라이브 Secret이 여전히 ownerRef(ES UID)를 가짐 | 같은 patch 루프를 rebuild-image DEPLOY=1 앞에 두거나, 1회성 운영 절차를 07 문서에 적기 |
| N-7 | S7: 라이브 수동 role `llm-gateway-dev-notification-worker-ses`가 terraform role_name과 같아서 `enable_notification_ses=true`로 apply하면 EntityAlreadyExists. import 안내가 없고 tfvars.example에 플래그도 없음. 변수 설명의 `scheduler.email.provider`는 틀린 키(`notificationWorker.email.provider`가 맞음) | `modules/irsa/main.tf`, `modules/irsa/variables.tf:143-148`, `environments/*/terraform.tfvars.example` | `aws iam get-role` 결과 존재, Tags null | example에 플래그와 `terraform import 'module.irsa.module.notification_worker_irsa[0].aws_iam_role.this[0]' <name>` 안내 추가, 설명 키 정정 |
| N-8 | S8: kube API 접근 실패(access entry 없음 등)면 `helm status` 실패를 "릴리스 없음"으로, `kubectl get ingress` 실패를 "Ingress 없음"으로 취급 → exit 0 → destroy 진행(fail-open). `terraform output`이 tf-init보다 먼저 돌아 미초기화 디렉터리에서도 스킵됨 | `tui/steps.py:86-106` | 코드 경로 | 먼저 `kubectl get ns llm-gateway` 도달성을 확인(NotFound만 스킵), helm status는 stderr의 "not found"로 구분 |
| N-9 | S9: `aws.allowed_roles: DevRole`(스칼라)을 문자 단위로 쪼갬. S9 신규 동작을 다루는 테스트 0건 | `docs/NDS/deploy/schema.py:389` | 실측 `['D','e','v','R','o','l','e']` → `ALLOWED_IAM_ROLES=D,e,v,R,o,l,e` | 스칼라면 `[str]`로 감싸거나 SchemaError + desired_env 테스트 |
| N-10 | 부분 처리: rebuild-image DEPLOY=0 안내문이 migration에도 `rollout status deploy/llm-gateway-migration`을 출력(N11). tui/README가 Tool GW를 여전히 기능으로 안내(N12). `values.yaml:369/422/585`의 "= 라이브(dev·prod)" 주석 미정정(N9). README의 CHANGE_ME 수가 prod 실제와 다름 | `rebuild-image.sh:148`, `tui/README.md:40`, `values.yaml:369` 등, `README.md:217` | stub 실행 출력 / grep | 해당 줄 정정 |
| N-11 | `patch_nextauth_url`이 `DEBUG_MODE=true`에서도 `--atomic`을 붙임. "실패 시 rollback 안 함" 계약과 충돌 | `install-eks.sh:628` | `ROLLBACK_FLAG`는 DEBUG 분기 전에 설정됨 | DEBUG_MODE면 rollback/cleanup 플래그 생략 |
| N-12 | N5를 "보류"로 보고했으나 diff가 prod `.terraform.lock.hcl`을 OpenTofu에서 terraform registry로 재생성(provider 패치 버전 상향 포함). 도구 결정이 조용히 내려진 상태. prod state가 tofu로 쓰였다면 tofu 사용자는 readonly init에 실패 | `environments/llm-gateway-prod/.terraform.lock.hcl` | diff | 되돌리거나, 결정으로 기록하고 처리 표를 정정 |
| N-13 | rebuild-image에 Helm ≥3.14 preflight가 없음. helm이 push 뒤에 실패하면(구버전 flag, atomic 롤백) 재실행이 동일 태그 가드에 막히고, 수동 명령도 출력되지 않음 | `rebuild-image.sh:64-69, 134-136` | 코드 경로 | 스크립트 상단에 helm 버전 검사, 실패 시 `HELM_CMD` 출력 |
| N-14 | 신규 테스트 공백: prod-loadtest overlay가 M1 파라미터에 없음(변이 통과). lifespan 40을 main.py에서 읽지 않고 하드코딩. HPA-min 가드(N-1) 미검증. prod PDB는 gateway 존재만 확인 | `tests/test_deployment_safety.py` | 변이 2건 PASS | 파라미터에 overlay 추가, `main.py`의 `+ 40.0`을 정규식으로 파싱, N-1 케이스 추가 |

### 범위 밖 관찰
- Compose tier에 `stop_grace_period`가 없다(docker 기본 10s). 이미지 기본 graceful 240s는 compose에서 의미가 없고 lifespan drain도 보장되지 않는다. 라운드 1 이전부터 있던 문제다.
- 처리 내역의 테스트 수치가 실측과 다르다(TUI "69 passed, 1 skipped" → 실측 101 passed, 신규 차트 테스트 "8" → 파라미터 포함 10). 결과에는 영향이 없다.

Not checked: prod 클러스터·IAM 실물(이 계정에 없음), 실제 Fargate eviction, 실제 롤아웃 중 ALB 절단 타이밍(TG 속성과 메커니즘으로만 판정), shellcheck(미설치), ECS 실배포.

Verdict: M1·M2·M4·M5·S1·S3~S9는 의도대로 동작함을 확인했다. M3 보존 로직과 S10에 경로상 구멍이 남았다. M1은 prod ALB와 ECS tier에서 효과가 상한에 막힌다. S-1~S-5를 고치면 수렴할 것으로 본다.

VERDICT: NOT CONVERGED
