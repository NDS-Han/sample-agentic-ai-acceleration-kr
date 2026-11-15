# 리뷰: doctor 점검

**범위**: `deploy/doctor.py` — check_eks/check_ecs/check_compose,
인스턴스 필터·배포성 준비·드리프트·위험 감지.

**판정: 수렴**

## 점검 계약

- k8s: deployment/job 준비성을 **릴리스 인스턴스 라벨로 한정** — 공유 ns 의
  다른 릴리스 pod 를 오진하지 않는다.
- terraform: managed overlay(gen tfvars)를 `-var-file` 로 준 plan —
  NDS 소유 키를 빠뜨린 plain plan 은 선언을 destroy 로 오진한다고
  별도 감시.
- helm: release 존재 vs helm 바이너리 부재 vs 클러스터 접속 실패를 구분.
- 부가: migration image head drift, HPA 커스텀 메트릭 도착, Helm
  managed-key drift(inbound-cidrs 부재 포함), ECR 태그 존재.

## 발견·수정된 결함

### HIGH

- **폴백 오흡수** (15차): `ours` 비어 무라벨 폴백 시 타 릴리스
  (`instance=<other>`) deployment/jobs 가 `component` 라벨만으로
  `by_name` 을 덮어씀 — 외부 pod 의 readiness/env 로 오진 → 폴백에서도
  타 릴리스 라벨 제외 + `setdefault` 첫-승(jobs 동일).
- **이름 프리픽스 매칭** (14차): fullnameOverride 시 deployment 식별
  실패로 전 서비스 `deployment 없음` 오진 → `component` 라벨 우선.
- **terraform plan 무init** (20차): 캐시된 backend 로 다른 env 의 state
  drift 를 계산해 정상 보고 → `_tf_init` reconfigure 후 plan.

### MEDIUM

- **migration 검증**: job conditions 우선, helm history 폴백;
  `containers[0]` 타입 가드·비-dict 아이템 isinstance 가드(18차).
- **ECS doctor**: `describe-services.failures` 확인(18차), `list-tasks`
  family:rev 스트립 + `-no-color` + returncode 체크 + 라이브 service
  이름 역매핑.
- **helm 부재/릴리스 부재 구분**: rc=127 → 설치 안내, "not found" →
  release 부재 — 혼동 시 release 부재로 착각해 인프라 판단 오류 방지.
- **plain-view 위험 경고**: managed overlay 없는 plan 이 NDS 소유 모듈
  (SES IRSA/body_logging)을 destroy 로 띄우면 별도 WARN — 운영자의
  `terraform apply` 사고 차단.
- **HPA custom metrics**: prometheus-adapter/OpenTelemetry k8sattributes
  도착 여부를 별도 확인 — metrics API 있어도 시리즈 0 이면 WARN.

## 회귀 테스트

`test_doctor.py` — 라벨 필터·폴백 오흡수·helm 구분·migration·failures·
비-dict 가드.

## 잔여 리스크

- doctor 는 읽기 전용이라 실패 방향은 오진(WARN) — destructive 경로 없음.
- `_tf_drift_plan_argv` 의 상세 플래그는 환경의 terraform 버전에 의존
  (LOW).

## 28차 적용

- **라벨 위조가 doctor 판정을 가림**(MEDIUM): instance 라벨 위조
  deployment/Job 이 readiness·이미지·migration 판정을 오흡수/은폐 —
  helm 매니페스트 기록 이름이 있으면 그것만 인정(레거시는 라벨 폴백),
  deployment·migration Job 양쪽에 적용.
