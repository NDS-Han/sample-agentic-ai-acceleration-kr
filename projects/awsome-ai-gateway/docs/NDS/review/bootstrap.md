# 리뷰: 최초 설치 (bootstrap)

**범위**: `deploy/bootstrap.py`, `deploy/cli.py` 의 `cmd_bootstrap`/
`_tfstate_with_retry`.

**판정: 수렴**

## 흐름

`cmd_bootstrap`: gateway.yaml 확보(init 통합) → `scaffold_eks_env`(env
디렉토리·tfvars·env values 사본) → `wizard_infra`+`persist_bootstrap_
answers` → render → `_confirm_apply` → `ensure_tfstate` → `env_apply`
(terraform saved-plan apply) → `update_kubeconfig` → `seed_secrets` →
`push_images_eks` → `eks_apply.apply` → `onboard_admin` → doctor.

## 발견·수정된 결함

### HIGH

- **stale in-memory cfg** (20차): `_tfstate_with_retry` 가 사용자 재입력을
  `persist_bootstrap_answers` 로 yaml 에 쓰지만 `cfg` 는 구값 — 이후
  `eks_apply.apply` 의 `_tf_init` 이 폐기된 backend 로 reconfigure 해
  managed apply/outputs 가 다른 state 에 쓰일 수 있었다 → 재시도 후
  `schema.load` 재로드.
- **scaffold_tfvars 리터럴 치환** (19차): `environment="dev"` 공백 변형
  등 템플릿 서식 변경에 `str.replace` 가 조용히 no-op → 신 env state 에
  `environment = "dev"` 잔존으로 다른 env 이름 리소스 생성 → 앵커
  정규식 + 미매치 경고 주석.
- **partial-scaffold** (19차): mkdir↔쓰기 사이 중단 시 반쪽 env dir 이
  완성으로 취급돼 variables.tf 기본값 plan → 기본 경로는 부족 파일을
  이어서 완성(tf_env_dir 지명 dir 은 사용자 소유라 noop 유지). 복구 시
  기존 terraform.tfvars(검토본)는 덮어쓰지 않음(20차).

### MEDIUM

- **onboard_admin 계정 대상** (19차): gen 레이어가 `adminBootstrap.emails`
  를 `oidc.admin_emails` 로 덮어쓰는데 env 파일만 읽어 Cognito 계정이
  어긋남 → 실효(gen) 값 우선.
- **env_apply saved-plan rc** (20차): `-detailed-exitcode` 계약 밖의
  rc(시그널 kill 등)도 apply 로 떨어지던 경로 → `rc not in (0,2)` 거부.
- **backend.tf 재작성**: 템플릿 dev key/region 하드코딩을 env 별로
  재작성 + assumed-role 세션 ARN 을 IAM role ARN 으로 정규화
  (EKS access entry 호환).
- **dev values 사본 중화**: `_sanitize_copied_values` 가 dev OIDC
  enabled/issuerUrl·계정 ARN·registry·role·COGNITO_REGION 을 중화
  (CRLF/flow/블록스칼라/주석 변형 포함, 15차까지 강화).
- **seed_secrets**: pending-deletion 시크릿은 배포 중단, 기존 시크릿은
  덮어쓰지 않음.
- **push_images_eks**: ECR repo 보장 → 서비스별 태그 해석(render 와 동일
  규칙) → build/push → scheduler pin 추가 빌드.

## 검증

`test_bootstrap.py` 48 tests — scaffold 재작성/부분 복구/정규식 내성,
persist 라운드트립, tfstate 재시도, 시드 멱등, onboard 우선순위.

## 21차 검증 라운드 (독립 리뷰어 — 추가 수정)

- **persist-skip split-brain** (MEDIUM): wizard 답변이 스키마를 깨면
  `persist_bootstrap_answers` 가 쓰기를 거부하는데, 호출자가 "설정 저장"
  을 출력하고 in-memory 의 커스텀 tfstate 이름으로 state 를 만듦 → 다음
  apply 가 yaml 의 기본 backend 로 reconfigure 해 상태 분리 → 함수가
  bool 을 반환하고 cli 는 **재로드한 선언/기본값**(`default_tfstate_names`)
  으로 수렴, 불일치 시 안내.
- **migration 미선언 태그 함정** (MEDIUM): `images.tag`/`tags.migration`
  미선언 시 helm 은 env overlay 핀(스캐폴드 dev 태그, 미 push)에 폴백해
  migration Job 이 ImagePullBackOff → `plan()` 이 `_migration_image` 로
  레이어 역산한 실효 태그도 ECR 존재 확인에 포함.
- **`infra:`/`features:` 명시 null 크래시** (LOW-MED): 스키마 유효값인데
  `setdefault` 가 None 을 돌려 AttributeError → 정규화 후 병합.
- **ecs 기본 lock table 미참조** (LOW): `ensure_tfstate` 가 만드는 기본
  table 을 backend.hcl 이 생략해 state unlock → 미선언 시 기본값 방출.
- **oidc 선언 + domain 없음 잠금** (LOW): hosted UI 는 https 콜백만
  허용해 redirect 거절 + dev_login 강제 off → 스키마 경고 추가.
- **AZ 폴백 메시지** (LOW): "리전 처음 두 AZ" 가 아니라 variables.tf 의
  ap-northeast-2 핀이 쓰인다는 정직한 안내로 수정.

## 잔여 리스크

- scaffold 완성 후 `SystemExit(검토 후 재실행)` 패턴은 의도된 검토 게이트 —
  자동화 파이프라인에서 쓰려면 `--yes` 흐름 문서화 필요(LOW).
- `list_azs`/`account_id` 가 STS/EC2 조회라 오프라인 환경에선 scaffold 가
  부분 실패 → 값은 주석으로 표면.
- `bootstrap.env_apply` 는 saved **전체-env** plan/apply — 재실행 시 사람의
  tfvars drift 도 함께 적용된다(managed 전용 `deploy apply` 와 다른 계약,
  패널/plan 스트림으로 공시).
- `module.eks`·`vpc_cidr`/`azs` 등 사람 소유 모듈은 managed 수렴 대상이
  아니다 — 변경은 env terraform 수동(경고로 안내).
