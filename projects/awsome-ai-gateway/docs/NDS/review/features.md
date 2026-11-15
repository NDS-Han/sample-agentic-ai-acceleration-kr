# 리뷰: 기능 플래그 + boto3 프로비저너

**범위**: `deploy/chatagent.py`(bi_insight AgentCore Runtime),
`deploy/websearch.py`(AgentCore 웹검색), `deploy/pricinglambda.py`
(LiteLLM 단가), `features.alerting`/`body_logging`/`notifications`/
`service_keys`/`observability` 배선.

**판정: 수렴**

## 프로비저너 계약

- helm/terraform 관리 밖의 리소스를 boto3 로 **멱등 보장/제거**.
- **입력 env 스크럽**: 프로비저너 호출 시 부모 프로세스의 AWS/프록시/
  시크릿 env 가 새로 만든 Runtime 의 env 로 새는 것을 차단
  (`_PROVISIONER_INPUTS` 제거 후 명시 키만 주입, 17차).
- **죽은 상태 재사용 금지**: FAILED/DELETING 리소스를 이름으로 재사용하면
  재생성 불가 데드락 → 상태 필터 + 신규 생성(17차).
- **trust 정책 수렴**: 기존 role 재사용 시 trust·BasicExecutionRole·
  invoke 정책을 멱등 부착.

## 발견·수정된 결함

### HIGH

- **env 누출** (17차): `_env` 빌더가 부모 env 를 통째로 상속해 시크릿/
  다른 env 설정이 Runtime env 로 유입 — 스크럽 + 명시 키만.
- **죽은 리소스 재사용** (17차): FAILED/DELETING Runtime/interpreter/
  gateway/target 을 재사용해 영영 준비 안 됨 → 상태 필터.
- **status_url 오판정** (17차): degraded gateway 도 "존재"로 판정해 자가
  치유 불가 → gateway READY + target 상태 동시 요구.
- **dirty worktree 태그** (17차): 소스 수정분이 같은 태그로 push 돼
  배포된 코드와 repo 가 어긋남 → dirty 해시 반영.
- **부분 실패 수렴**: role 존재로 판정해 정책 부착을 영구 스킵 → 멱등 부착.

### MEDIUM

- **plan() fn_name 불일치** (13차): 상태 문자열을 `--set` 으로 넣어
  preview↔apply 불일치 + wipe 오인.
- **프로비저너 실패 시 라이브 보존**: 실패로 URL/ARN 못 얻으면 `--set` 빈
  값으로 라이브 값 wipe → 이전/라이브 값 보존.
- **AGENTCORE_REGION sanitize 오치환**: websearch 커넥터 상수 `us-east-1`
  을 배포 리전으로 덮어쓰던 경로 — 커넥터 상수만 보존(15차).
- **`admin_role_arn` 부재 경고**: invoke 정책 부착 대상 없으면 명시 경고.
- **`_agent_name` 도출**: env-고정 이름 규칙 + AgentCore 47자/IAM 64자
  제한 스키마 경고(18자/24자 안내).
- **boto3 부재 가드**: `status()` 의 ImportError → 친절한 메시지.
- **alerting**: 이메일 구독은 managed overlay 소유 — `alert_emails` 는
  gen overlay 에만 있어 capture 복원 필수(20차 gen 경로 수정으로 정합).

## 회귀 테스트

`test_pricinglambda.py`/`test_websearch.py`/`test_bootstrap.py` —
env 스크럽·READY 요구·신뢰 정책·격리·fn_name·보존 경로.

## 잔여 리스크

- AgentCore API 는 리전별 신기능 — 스키마의 `mantle_regions` 검증은
  문자열 패턴만 확인, 실제 가용 리전 목록은 라이브 확인 필요.
- 프로비저너의 부분 생성 상태(중간 실패)는 다음 apply 가 수렴 —
  완전 정리는 teardown 에 의존.
