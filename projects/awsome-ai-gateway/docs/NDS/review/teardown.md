# 리뷰: teardown (삭제)

**범위**: `deploy/cli.py` 의 `_teardown_eks`/`_teardown_ecs`/`_teardown_compose`/
`_confirm_destroy`, 프로비저너 teardown(chatagent/websearch/pricinglambda).

**판정: 수렴**

## 안전 계약

- 확인은 y/n 이 아니라 **환경 이름을 그대로 타이핑** — 비-TTY 는 `--yes`
  필수.
- `--infra` 면 `terraform plan -destroy` 미리보기를 먼저 보여준다.
- `env_dir` 의 `environment` tfvar 가 `cfg.env` 와 다르면 destroy 거부;
  판독 불가(공유/채택 env)면 확인 문자열을 **env_dir 이름**으로 묶어
  state 전체 삭제 스코프를 명시(19차).
- helm release 부재/helm 부재/접속 실패를 구분 — 앱이 살아 있는 상태로
  인프라 삭제를 진행하지 않는다.

## 발견·수정된 결함

### HIGH

- **Runtime 누수** (13·14·16차): `chat_agent_name` 을 destroy 전 outputs
  에서 실측해 전달 — 사람이 `project` 변수를 바꾼 env 에서 도출 이름과
  실측이 어긋나 프로비저너 Runtime 이 state 밖에 새어나가던 문제
  (eks·ecs 동일 적용).
- **init 없는 destroy** (19차): teardown 의 `terraform init` 이 저장된
  `.terraform` 에만 의존해 캐시 부재/낡은 backend 면 실패하거나 다른
  state 대상이 됨 → `_tf_init`(선언 backend·reconfigure)으로 교체.
- **프로비저너 실패 시 라이브 값 wipe** (16차): 실패로 URL/ARN 을 못
  얻으면 `--set` 에 빈 문자열이 들어가 라이브 AGENTCORE_* 를 지움 →
  보존 경로(라이브 값 유지) 확보 + `_live_deploy_env` 라벨/사이드카 대응.

### MEDIUM

- **단계별 오류 격리** (17차): 프로비저너 teardown 이 한 단계 실패로 전체를
  멈추지 않고 계속 + 페이지네이션(NotFound 타입 기반 판정).
- **stale alerts-dims 정리**: uninstall 후 gen 의 stale ALB dimension 이
  재배포 시 죽은 ALB 알람을 만들지 않게 삭제.
- **수동 힌트**: teardown 실패 시 `ROLE_NAME` 포함한 수동 삭제 명령 안내.
- **deletion protection 경고**: Aurora prod/provisioned 의 deletion
  protection 켜진 경우 실패 안내.
- **`human_owned` 탐지** (15차): terraform 은 `"true"` 인용도 bool 강제
  변환 — `*.tfvars.json`+`TF_VAR_*` 전 채널 검사로 사람 소유 Runtime 을
  매 apply 삭제/재생 루프에서 보호.

### 21차 검증 라운드 (독립 리뷰어 F1~F6)

- **pricing 정책 잔여 → destroy 중간 실패** (F1): 플래그 off 면 detach 를
  건너뛰어 `LitellmPricingInvoke` 인라인 정책이 terraform 소유 role 에 남아
  `DeleteRole` DeleteConflict → 플래그 무관하게 `admin_api_role_arn`
  존재 시 teardown 시도.
- **out-of-band 잔여물** (F2): terraform state 밖 리소스가 teardown 에
  안 잡힘 → `_teardown_eks_oob` 가 ESO SecretStore·seed secrets
  ({env}/app·/redis, 7일 유예)를 자동 정리, ECR repo·ACM cert·R53
  레코드·비어있지 않은 버킷은 패널 공시(소유 모호/삭제 범위).
- **feature-off 누수** (F3): web_search/pricing_lambda 에 bi_insight 와
  같은 off-teardown 추가 — 라이브 env 주입 흔적이 있을 때만(채택 오타겟
  방지).
- **env_dir 가드 전 채널화** (F4): `terraform.tfvars` 한 파일만 보던 가드
  → `_declared_environment` 가 `*.auto.tfvars`·`*.tfvars.json` 전부 스캔
  (주석은 tfvars_clean 제거).
- **apply 경로 동일 가드** (F6): teardown 에만 있던 env 검증을 apply 에도
  — 선언 불일치 env_dir 에 managed 수렴이 쓰기 전에 거부.
- **빈 outputs + 살아있는 release** (F5): wrong-bucket state 에 붙어
  `outputs={}` → 전부 "미생성" → required apply 가 다른 state 에 중복
  생성 → release 생존 + managed 대상 있으면 apply 거부.

## 회귀 테스트

`test_eks_apply.py`/`test_bootstrap.py` — scope 가드·실측 이름·격리·
힌트·dims 정리·전 채널 env 스캔·불일치 거부.

## 잔여 리스크

- ECS teardown 은 배포 보류 경로라 코드 수준 검증만 — 라이브 실행 필요.
- destroy 는 terraform state 단위 — ECR 이미지·ACM cert·R53 레코드·
  수동 리소스는 패널에 공시하고 수동 삭제.
- managed plan 의 in-scope `update` 액션은 무조건 허용 — 사람의 env
  tfvars 변경이 target 모듈 안이면 NDS apply 에 동승한다(메시지로 공시).
- terraform workspace 사용 채택 env 는 `.terraform/environment` 의 선택
  워크스페이스가 env 가드 밖이다(LOW).
