# 리뷰: deploy upgrade eks (EKS 홉 단위 업그레이드)

**범위**: `deploy/eks_upgrade.py` 전체, `deploy/cli.py`의 `cmd_upgrade`/
서브파서, `deploy/schema.py`의 `infra.eks_version`, `deploy/capture.py`의
버전 역생성, `deploy/eks_apply.py`의 `_warn_version_drift`.

**판정: 수렴** — opus-5.5 adversarial 리뷰(차단 2·중요 8) 전량 반영 후
실환경 1.34→1.35 업그레이드 완주 검증.

## 구조

- `apply`는 클러스터 버전을 건드리지 않는다 — 버전 변경은 순서 있는
  절차형 작업이라 전용 명령이 담당. 라이브 → 목표까지 minor 한 칸씩.
- 홉: tfvars 기록 → `terraform plan -out` + 범위 검사 → 저장된 plan apply
  → ACTIVE 대기 → 워크로드 전량 재생성 + `rollout status` 대기.
- 애드온 버전: `describe-addon-versions`의 목표 k8s `defaultVersion`이
  기본이되 **현재 핀이 호환이면 유지** — 비호환 고핀의 자동 다운그레이드는
  중단.
- 선언: `infra.eks_version`(yaml, 기대값·drift 감지용) ↔ 실제 적용은 env
  `terraform.tfvars`(eks_cluster_version/eks_addon_versions) — 완료 시
  yaml을 라이브로 갱신해 둘을 일치시킨다.

## 발견·수정된 결함 (외부 리뷰 지적)

### 차단

- **kubectl 플래그 오인자**: `--kube-context`(helm 플래그)를 kubectl에
  전달 → 지정 시 실패. 미지정 시엔 current-context가 다른 클러스터면 그
  클러스터 전체를 재시작할 수 있던 경로 → `--context`로 교정 +
  `_verify_context`가 kubeconfig 해석 클러스터≠대상 클러스터면 차단
  (current-context도 검사, 판독 불가는 경고만).
- **plan 미검증 -auto-approve**: managed apply의 `_inspect_managed_plan`
  보호가 없어 module.eks 의존성 drift·모듈 내 파괴 변경까지 적용 가능했고,
  tfvars 부재 머신에선 access_entries 기본값 리셋→관리자 잠김 위험 →
  `plan -out` 저장 → `_inspect_upgrade_plan`(module.eks 밖·destroy/
  replace·moved/import 차단, `time_sleep` 재생성만 예외) → 저장된 plan만
  apply. tfvars 부재 시 중단.

### 중요

- **반쪽 홉 재개 함정**: 컨트롤플레인만 올라가고 애드온/파드 단계 실패 시
  재실행이 "이미 최신"으로 빠져나가 영구 누락 → 라이브=목표여도
  `describe-addon` 실측과 비교해 남은 수렴부터 이어감.
- **대기 조건 오인**: `kubectl wait --for=condition=available`은 구 파드가
  Available=True를 유지해 롤아웃 완료를 기다리지 못함 → 워크로드별
  `rollout status` 대기, 대상을 deployment+statefulset으로 확장.
- **tfvars 정규식 파괴**: 한 줄 맵/들여쓰기/말줄 개행 없는 형태에서
  `^eks_addon_versions\s*=\s*\{.*?^\}`가 후속 블록을 지우거나 중복 정의
  유발 + 비원자 쓰기 → 브레이스 균형 스캔(`_strip_map_key`) + `.bak` +
  임시파일→os.replace 원자 쓰기.
- **변수 로드 순서 덮어쓰기**: `*.auto.tfvars`/`-var-file`이 버전 키를
  덮으면 "No changes" 후 50분 헛대기 → plan에 변경이 없는데 버전도
  미적용이면 즉시 중단.
- **애드온 다운그레이드**: `defaultVersion`이 운영자 핀보다 낮으면
  다운그레이드 시도 → 현재 버전 호환이면 유지, 비호환 고핀은 수동 판단
  요구로 중단.
- **사전 점검 부재**: upgrade insights ERROR(UpdateClusterVersion 차단
  사유) 미확인 + `_check_env_dir_declared` 미적용 → 사전 점검 추가.
- **blast radius**: StatefulSet 누락·전체 동시 재시작 → sts 포함 +
  kube-system 우선 순서로 개별 status 대기.

### 실환경 회귀

- **time_sleep 오탐** (첫 실행에서 발견): terraform-aws-eks의 데이터플레인
  대기 리소스는 버전 변경마다 항상 create+delete → 파괴적 변경 검사에서
  예외 처리. 이후 실환경 1.34→1.35 완주(컨트롤플레인 6분, 워크로드 17개
  재생성·status 통과, yaml 자동 갱신).

## 잔여 인지 사항

- `-y`는 확인 생략(CI용) — 대화형 경로의 프롬프트가 기본 안전망.
- Fargate 플랫폼 kubelet은 AWS 점진 롤아웃이라 업그레이드 직후
  재생성 파드도 구 kubelet에 배정될 수 있음 — n-1 skew는 지원 범위.
- ALB controller·External Secrets 차트, Aurora/ElastiCache 엔진은 범위 밖
  (별도 작업 — upgrade-eks-cluster.md §3).
