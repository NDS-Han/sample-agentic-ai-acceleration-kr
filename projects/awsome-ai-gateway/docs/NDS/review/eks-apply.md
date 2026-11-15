# 리뷰: EKS apply (helm + managed terraform 수렴)

**범위**: `deploy/eks_apply.py`(`plan`/`apply`/`ensure_infra`/
`_apply_managed`/`_managed_infra_plan`/`_inspect_managed_plan`/`_tf_init`/
`dynamic_set_args`/`_helm_argv`/`tf_outputs`/`ensure_cluster_secret_store`/
`ensure_domain_*`/`live_*` 계열), `deploy/render/eks.py`.

**판정: 수렴**

## 구조

- helm: values 레이어(base 차트 → env overlay → gen managed) + `--set` —
  terraform output(DB·Redis·IRSA·Cognito·ALB dims)·프로비저너 결과·도메인/
  이미지 태그를 주입한다.
- terraform: yaml 소유 모듈만 `-target` 으로 좁힌 managed 수렴 —
  `_inspect_managed_plan` 이 저장된 plan 의 변경 대상을 화이트리스트로
  검증해 대상 밖 변경·비허용 delete/replace 를 거부한다.

## 발견·수정된 결함

### HIGH·CRITICAL

- **terraform backend 일관성** (19·20차): `.terraform` 캐시된 낡은 backend
  로 plan/apply/doctor/teardown 이 다른 env state 에 붙던 문제 → 모든
  경로 `_tf_init` 이 선언 backend 로 `-reconfigure`(ecs/doctor/teardown 도
  동일). managed 대상이 없어도 stale outputs 가 `dynamic_set_args` 에
  흐르던 경로 차단.
- **admin_internal 드롭** (16차): 도메인 설정 시 per-ingress dict 통째
  교체로 scheme 어노테이션 유실 → internal 선언이 internet-facing 으로.
- **selfsigned-temp 데드락** (16차): cert 생성을 ALB 존재에 묶어 첫 설치가
  ALB 영영 안 뜨는 상태 → cert 를 ALB 독립적으로 보장(안정 CN+와일드카드).
- **nextauthUrl 2-phase 덮어쓰기** (16차): env 선언 URL 을 ALB 발견값이
  덮어쓰던 문제 → 선언 스캔 게이트.
- **SecretStore kind** (13·14차): `ClusterSecretStore` 만 생성하던 것이
  차트의 namespaced `SecretStore` 선언과 불일치 → kind 정규화. 단
  namespaced 는 same-ns IRSA SA 가 필요하고 차트가 제공 안 해 자동 생성
  불가 → 생성 대신 경고+스킵(인증 불가 manifest 생성 방지).
- **ECR 태그 fail-fast** (15차): `deploy apply` 는 이미지를 push 하지
  않는데 `images.tag` 만 올리면 helm 은 성공해도 pod 는 ImagePullBackOff —
  preview(항상 실행)에 `describe-images` 체크로 확인 단계 경고.

### MEDIUM

- **managed plan 화이트리스트**: 비대상 변경·delete/replace 거부 —
  ALB-alarm(라이브 dims 발견 후만)·email 구독·member-alarm 축소만 예외.
- **cognito_groups/callback 병합**: env tfvars 와 managed 선언의 병합 누락
  시 그룹 delete plan 이 가드에 걸려 매 apply 중단 → 병합.
- **strip_es_owned_secrets**: 차트 기본 시크릿명(`llm-gateway-db` 등 fullname
  무관) 미커버 → 기본값 포함. 스칼라 섹션 `AttributeError` 가드.
- **vpc/azs 경고 + 서브넷 산출**: env 선언 azs 개수와 overlay 방출 개수
  불일치 → render_tfvars 가 실제 선언 개수만큼만 방출.
- **_find_domain_cert**: ISSUED 우선→PENDING 순 정렬 + 와일드카드 SAN 요구
  + 재사용 cert Status 확인(만료/비활성 재사용 차단).
- **_live_deploy_env**: 프로비저너 실패 시 라이브 ARN/URL 보존 경로가
  `{release}-admin-api` 이름 고정 + containers[0] — fullnameOverride·
  사이드카 대응(라벨 매칭+서비스명 컨테이너).
- **pricing fn_name**: plan() 이 상태 문자열("Active"/"미배포")을 `--set`
  으로 넣어 preview↔apply 불일치 + wipe 오인 → 함수명 주입 정렬.
- **migration 이미지**: 차트 appVersion 폴백 제외(독립 핀), values 레이어
  last-wins 병합으로 실제 배포 URI 와 동일 계산.

## 검증

`test_eks_apply.py` 80+ tests — 라벨 필터·서비스명 컨테이너·SecretStore·
_tf_init reconfigure·load_meta·managed plan·teardown 가드·ECR 태그.

## 21차 검증 라운드 (독립 리뷰어 — 추가 수정)

- **apply-path env_dir 가드** (F6): teardown 에만 있던 environment 선언
  검증을 apply 에도 — `_declared_environment` 가 terraform 자동로드
  채널 전부(tfvars/auto.tfvars/tfvars.json, 주석 제거)를 스캔해 선언
  불일치 env_dir 에 managed 수렴이 쓰기 전에 SystemExit. plan() 은 경고.
- **빈 outputs + 살아있는 release 차단** (F5): tfstate_bucket 이 존재
  하지만 이 env state 가 아니면 `outputs={}` → `_infra_missing` 전량
  미생성 → required apply 가 중복 생성하다 이름 충돌 mid-fail → helm
  release 생존 + managed 대상이 있으면 apply 거부(외부-인프라 경로는
  missing 이 비므로 통과).
- **web_search/pricing_lambda off-teardown** (F3): bi_insight 에만 있던
  off 수렴을 나머지 프로비저너에도 — 라이브 env 주입 흔적이 있을 때만
  teardown(채택 배포의 사람 소유 동명 리소스 오타겟 방지), 없으면
  AgentCore Gateway/Lambda·role·인라인 정책이 영구 잔여.
- **managed-plan 잔여 관찰** (문서화): `module.irsa` target 이 사람의
  `enable_chat_agent` 선언을 dep 으로 끌어와 `module.agentcore_runtime`
  update 가 plan 에 섞이면 out-of-scope `bad` 로 매 apply 중단 —
  fail-closed 가 의도이며 env tfvars 정렬까지 반복된다(스킵하지 않음).

## 잔여 리스크

- `_infra_targets` 커버리지는 yaml 선언 모듈 전체를 대상으로 설계됐으나
  신규 managed 모듈 추가 시 `_infra_targets`/`_inspect_managed_plan`/
  `render_tfvars` 3곳 동기화가 필요 — 신규 모듈은 리뷰 체크리스트에 명시.
- `human_owned` 탐지는 tfvars 정규식 기반 — HCL 문법의 극단적 변형
  (heredoc, 멀티라인 주석 사이 선언)은 미스 가능(LOW, fail-open 방향이
  아니라 사람 소유로 오인해 보수적 중단).

## 23차 적용 (독립 리뷰 — 추가 수정)

- **`aws.allowedIamRoles` 미방출**(HIGH): eks desired_values 가 이 키를
  쓰지 않아 `ALLOWED_IAM_ROLES=""` → STS CLI 로그인 전량 403(fail-closed).
  방출 + 빈 선언 시 feature_note 경고.
- **plan() 의 pre-consent alias**(HIGH): preview 단계의
  `update-kubeconfig --alias` 가 context 를 backend 클러스터로 덮어써
  apply 의 hard 검사를 무력화 + `--plan` 이 kubeconfig 를 변형 — alias 전
  hard 대조로 변경.
- **`_env_tfvars_texts` JSON 채널**(MEDIUM): `terraform.tfvars.json`/
  `*.auto.tfvars.json` 도 자동로드인데 스캔이 HCL 만 봐 거기만 선언된
  cognito_groups/콜백/vpc_cidr/AZ 가 gen 오버레이에 통째 교체로 지워졌다 —
  terraform 로드 순서로 펼쳐 반환.
- **teardown context 가드**(HIGH): helm status/uninstall 전에 context↔
  backend cluster_name 대조 + outputs 없으면 검증 생략을 명시 경고.
- **CHAT_AGENT_REGION 검증**: tfvars/--set 직행 env 값에 리전 형식 검증.
- **environment 비인용 값**: `environment = 5` 같은 tfvars 허용 형태도
  '선언됨'으로 인식(과잉 통과 아닌 과잉 차단 방향).
