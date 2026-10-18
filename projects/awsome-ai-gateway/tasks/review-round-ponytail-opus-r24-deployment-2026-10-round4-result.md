# R24 리뷰 라운드 4 — 수정 검증 결과

대상: 라운드 3 처리분(S-6, S-7, N-8 잔여, N-15, N-16, N-17) + 라운드 1~3 누적 diff 회귀 스윕.
방법: 모든 판정은 실행으로 확인했다. bash 3.2 는 `docker run bash:3.2`(3.2.57) 에서 aws/helm 스텁과
임시 git 리포를 써서 `install-eks.sh` 의 `helm_install` 을 끝까지 실행했다. 차트 변이는 원본을 백업한 뒤
주입하고 복원했다(복원은 cmp 로 확인).

## 요약

| ID | 판정 | 심각도 |
|---|---|---|
| S-6 | install-eks.sh 는 해결됨. **rebuild-image.sh:37 에 같은 `declare -A` 가 남아 있음** | **S (잔여)** |
| S-7 | 원래 오탐(mig>app)은 해결됨. 분기 상태에서 mig 쪽에만 db/ 변경이 있으면 여전히 차단(과차단) | N (잔여) |
| N-8 | env 주입은 동작함. 다만 테스트로 고정돼 있지 않고, init 실패 시 여전히 fail-open | N (잔여) |
| N-15 | 해결됨 (11개 입력 × bash 5/3.2) | — |
| N-16 | 3개 서비스에서 변이를 잡음. admin-ui·cost-recorder-worker 는 아직 테스트되지 않음 | N (잔여) |
| N-17 | 해결됨 (README "CHANGE_ME 마커를 전부 검색") | — |
| 스윕 | 새 회귀 없음. 사소한 사항 2건(아래) | N |

테스트 기준선: 차트 `pytest deployment/charts/llm-gateway/tests` 44 passed, TUI `pytest deployment/tui/tests` 101 passed.

---

## 1. S-6 — bash 3.2

**install-eks.sh: 해결됨.**
- `bash -n` 은 bash 3.2 에서 install-eks/rebuild-image/smoke-test 모두 OK. 단, `declare -A` 는 런타임 오류라서 `-n` 으로는 잡히지 않는다.
- bash 3.2 에서 `helm_install` 을 실제로 실행했다(스텁). `IMAGE_TAG` 경로의 ECR 조회 순서는
  `gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker migration admin-api` 로 7개이고, rc=0, helm upgrade 를 1회 호출했다.
- scheduler→admin-api 매핑은 `values.yaml:869` (`scheduler.image.repository: "llm-gateway/admin-api"`) 와 맞는다. env 별 values 는 repository 를 override 하지 않는다.
- admin-ui 태그가 없는 상황(ImageNotFound 스텁)에서는 `llm-gateway/admin-ui:<tag>` 를 출력하고 exit 1 로 중단한다. 정상 동작이다.
- `${sts_current//,/$'\n'}`(3.2 에서 ANSI-C quoting 이 다르게 동작하는 부분)도 3.2 에서 `ap-northeast-2,us-east-1,us-west-2` 로 맞게 나온다.

### [S] S-6r4 — rebuild-image.sh 에 연관 배열이 남아 있음
- 파일:라인: `deployment/scripts/rebuild-image.sh:37-46` (`declare -A VALUES_KEY`, `${!VALUES_KEY[*]}`)
- 재현 (bash:3.2):
  ```
  $ AWS_REGION=ap-northeast-2 bash rebuild-image.sh gateway-proxy dev
  rebuild-image.sh: line 37: gateway: unbound variable      # rc=1
  ```
  bash 3.2 는 `declare -A` 를 인덱스 배열로 처리한다. 그래서 `[gateway-proxy]` 가 산술식으로 평가되고, `set -u` 상태에서 즉시 종료된다. macOS 기본 셸에서는 모든 서비스의 재빌드가 불가능하다. 라운드 3 에서 고친 것과 같은 유형의 결함이고, R24 가 안내하는 "rebuild-image DEPLOY=1" 경로가 바로 이 스크립트다. 이 상태에서는 N-15 의 버전 가드에도 도달하지 못한다.
- 최소 수정:
  ```bash
  values_key() {
    case "$1" in
      gateway-proxy) echo gatewayProxy ;; admin-api) echo adminApi ;;
      admin-ui) echo adminUi ;; notification-worker) echo notificationWorker ;;
      cost-recorder-worker) echo costRecorderWorker ;; migration) echo migration ;;
    esac
  }
  KEY="$(values_key "$SVC")"
  [ -n "$KEY" ] || { echo "❌ 알 수 없는 서비스: $SVC  (가능: gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker migration)"; exit 1; }
  ```
  재발을 막으려면 `deployment/scripts/*.sh` 를 대상으로 `docker run bash:3.2` 스모크 테스트를 추가하거나, `grep -n 'declare -A'` 가드 테스트를 둔다.

## 2. S-7 — migration/app 게이트 시나리오

임시 리포(c1 → c2[db 변경] → c2b[앱 변경], 분기 c1 → c3[앱 변경] → c4[db 변경])를 만들고, bash 3.2 에서 실제 `helm_install` 을 실행했다.

| 시나리오 | mig | app | 결과 | 기대 |
|---|---|---|---|---|
| (a) mig>app (db 차이 있음) | c2 | c1 | PASS | PASS ✓ |
| (b) app>mig + db/ | c1 | c2 | BLOCK rc=1 | BLOCK ✓ |
| (b') app>mig, db/ 변경 없음 | c2 | c2b | PASS | PASS ✓ |
| (c1) 분기, 앱 쪽에 db/ 변경 | c2 | c4 | BLOCK | BLOCK ✓ |
| (c2) 분기, mig 쪽에만 db/ 변경 | c2 | c3 | **BLOCK** | PASS 여야 함 |
| (d) mig=app | c2 | c2 | PASS | PASS ✓ |

### [N] S-7r4 — 분기 상태 과차단 (fail-closed)
- 파일:라인: `deployment/scripts/install-eks.sh:571-572`, 같은 유형 `deployment/scripts/rebuild-image.sh:96`
- (c2) 는 의미상 (a) 와 같다. migration 이미지가 앱이 모르는 리비전을 더 갖고 있을 뿐이다. 그런데 two-dot diff 는 양쪽 변경을 모두 보기 때문에 차단된다. 안전 쪽으로 실패하므로 N 으로 분류했다.
- 최소 수정: is-ancestor 와 two-dot 두 조건을 three-dot 하나로 바꾼다. three-dot 은 merge-base 에서 app 까지의 변경, 즉 앱 쪽에서만 바뀐 db/ 만 본다.
  ```bash
  && ! git -C "$DEPLOY_DIR/.." diff --quiet "${mig_tag}...${app_tag}" -- db/ 2>/dev/null; then
  ```
  위 6개 시나리오에 three-dot 을 적용하면 (a) PASS, (b) BLOCK, (c1) BLOCK, (c2) PASS, (d) PASS 로 모두 기대와 일치한다(실측). ancestor 검사는 이 조건에 포함되므로 지워도 된다. rebuild-image.sh:96 은 `"$live_mtag...HEAD"` 로 고친다.

## 3. N-8 — teardown `terraform init $TF_BACKEND_ARGS`

- env 주입: `steps.py:141-142` 가 `TF_BACKEND_ARGS` 를 넘기고, `runner.run_step` 은 `os.environ` 위에 `step.env` 를 덮어쓴다(runner.py:27-29). 따라서 실제로 전달된다.
- **테스트 근거가 없다.** `test_steps.py` 에 `TF_BACKEND_ARGS` 를 확인하는 assert 가 없다. 변이(env 에서 키 삭제)를 주입해도 **101 passed** 가 그대로 나왔다.
- 미설정 상태로 bash 를 직접 실행한 경우(실제 terraform + dev backend.tf): init 이 `The attribute "bucket" is required` 로 실패한다. 이 실패는 `|| true` 로 묻히고, 스크립트는 "state/output 없음 — 스킵" 을 출력한 뒤 **rc=0** 으로 끝난다.
- init 실패 스텁(AccessDenied)에서도 "스킵" 후 **rc=0** 이다. 즉 여전히 fail-open 이다.
  TUI 에서는 다음 `tf-init` 스텝이 같은 인자로 다시 실패하므로, 결정적 오류라면 destroy 까지 가지 않는다. 그러나 일시적 오류(throttle, 네트워크, lock)라면 helm 단계만 스킵되고 destroy 가 진행된다. 그러면 ALB 가 고아로 남아 VPC 삭제가 DependencyViolation 으로 실패하는 R24-S8 상황으로 다시 이어진다.

### [N] N-8r4
- 파일:라인: `deployment/tui/steps.py:90`, `deployment/tui/tests/test_steps.py`
- 최소 수정:
  ```bash
  : "${TF_BACKEND_ARGS:?TF_BACKEND_ARGS 미설정 — TUI 경유로 실행할 것}"
  terraform init -input=false -reconfigure $TF_BACKEND_ARGS >/dev/null 2>&1 || {
    echo "ERROR: terraform init 실패 — 클러스터 유무 판단 불가, destroy 중단" >&2; exit 1; }
  ```
  bootstrap 이 끝난 환경이라면 미배포 상태에서도 init 은 성공한다. 이때 output 은 `{}` 이고 `cluster_name` 이 없으므로 기존 스킵 경로를 그대로 탄다. 그래서 정상 스킵 동작은 회귀하지 않는다.
  테스트 추가: `assert "-backend-config=bucket=b" in helm.env["TF_BACKEND_ARGS"]`.

## 4. N-15 — helm 버전 파싱 (bash 5 / bash 3.2 결과 동일)

| 입력 | 파싱 결과 | MAJOR_OK |
|---|---|---|
| helm 미설치 | `` | 0 (중단) |
| `v3.16.2` | `3 16` | 1 |
| `3.16` | `3.16` | 0 (중단 — 비숫자) |
| `garbage` | `garbage` | 0 |
| `v3.13.9` | `3 13` | 0 |
| `v3.14.0` | `3 14` | 1 |
| `v4.0.1` | `4 0` | 1 |
| `v3.16.2+gabc` | `3 16` | 1 |
| `` / `v3` / `* *` | — | 0 |

해결됨. `3.16`(v 접두 없음)은 중단되지만, `helm version --template '{{.Version}}'` 은 항상 `v` 접두를 붙이므로 fail-closed 로 둬도 된다. 다만 bash 3.2 에서는 S-6r4 때문에 이 코드까지 도달하지 못한다.

## 5. N-16 — PDB 테스트 변이 주입

변이 내용: 5개 `pdb.yaml` 의 `ternary(HPA min, replicaCount, autoscaling.enabled)` 를 `gt replicaCount minAvail` 로 되돌렸다. 즉 HPA 가 켜져 있어도 replicaCount 를 기준으로 PDB 를 렌더한다.
- 5개 서비스 모두에 변이를 넣으면 `test_pdb_not_rendered_when_hpa_min_le_min_available[adminApi|gatewayProxy|notificationWorker]` 가 **3 failed** 로 실패한다. 의도대로 동작한다.
- admin-ui·cost-recorder-worker 에만 변이를 넣으면 **17 passed** 로 통과한다. 이 두 서비스는 테스트되지 않는다.

### [N] N-16r4
- 파일:라인: `deployment/charts/llm-gateway/tests/test_deployment_safety.py:201-216`
- 문제 1: 이 테스트가 덮지 못하는 서비스가 2개 있다(prod 에서 실제로 PDB 가 렌더되는 서비스는 5개).
- 문제 2: docstring 은 "replicaCount=3 + HPA min=1" 이라고 하지만 replicaCount 를 고정하지 않는다. 그래서 prod values 의 rc(3/3/2)가 minAvailable 보다 크다는 사실에 기대고 있다. 또 양성 대조가 없다. 템플릿이 아예 렌더되지 않게 망가져도 이 테스트는 통과한다.
- 최소 수정:
  ```python
  @pytest.mark.parametrize("key,suffix", [
      ("adminApi","admin-api"), ("gatewayProxy","gateway-proxy"),
      ("notificationWorker","notification-worker"), ("adminUi","admin-ui"),
      ("costRecorderWorker","cost-recorder-worker"),
  ])
  def test_pdb_follows_hpa_min_not_replica_count(key, suffix):
      base = ("values-eks-fargate-prod.yaml", f"{key}.replicaCount=3",
              f"{key}.podDisruptionBudget.enabled=true", f"{key}.podDisruptionBudget.minAvailable=1")
      on = _pdb_names(_render(*base, f"{key}.autoscaling.enabled=false"))
      assert any(n.endswith(f"-{suffix}") for n in on)          # 양성 대조
      off = _pdb_names(_render(*base, f"{key}.autoscaling.enabled=true", f"{key}.autoscaling.minReplicas=1"))
      assert not any(n.endswith(f"-{suffix}") for n in off)
  ```

## 6. 누적 diff 회귀 스윕

| 항목 | 결과 |
|---|---|
| `set -- $HELM_MAJOR` 이후 위치 인자 (rebuild-image.sh:108~) | 108행 이후 `$1/$2` 는 파싱 case 에서만 쓴다. SVC/ENV 는 27-28행에서 이미 변수에 담았다. 회귀 없음 |
| `values_tag`/`ecr_repo`/`IMAGE_KEYS` 스코프 | 모두 `helm_install` 안에서 선언했지만 `local` 이 아니다. 함수 정의는 첫 호출 뒤 전역이 된다. 다른 함수의 `missing` 은 `local` 이라 충돌하지 않는다. 스크립트는 한 번만 실행되므로 기능 문제는 없다(선택 사항: `local live_values live_tag mig_tag app_tag key repo out missing`) |
| `values_tag` 정확도 | dev/prod 에서 `migration=1.0.54-us19`, `gatewayProxy=1.0.87-us19` 로 yq 결과와 일치 |
| `helm_err=$(...) && helm_ok=0 \|\| helm_ok=1` + `set -e` | bash 5 와 3.2 모두에서 실패해도 계속 진행하며 `helm_ok=1`, 메시지도 캡처된다. 성공 시 `helm_ok=0`. 회귀 없음 |
| teardown "not found" 판정 | ns 검사(`NotFound` 를 공백 없이 매칭)가 먼저 kube API 도달성을 확인한다. 따라서 exec-plugin "not found" 같은 오인이 helm 단계까지 가지 않는다 |
| notification-worker automount | install-eks 의 `--set-string notificationWorker.serviceAccount.annotations.eks\.amazonaws\.com/role-arn=…` 이 만드는 키와 `hasKey … "eks.amazonaws.com/role-arn"` 이 정확히 일치한다. 렌더 결과: 어노테이션 없음 → `false`, ARN 있음 → `true`. 오타 없음 |
| bash 3.2 빈 배열 + `set -u` | 3.2 는 `"${a[@]}"` 가 빈 배열이면 unbound 로 죽는다(실측). install-eks 의 모든 `[@]` 확장은 비어 있지 않은 분기 안에 있거나, 항상 채워진 배열이다(SET_ARGS, IMAGE_KEYS). 안전 |
| 기타 bash4 전용 문법 | `mapfile`/`readarray`/`${x,,}`/`local -n`/`\|&`/`;;&` 를 grep 했다. rebuild-image.sh:37 외에는 없다 |

### [N] SW-1 — `ecr_repo` 기본 분기 없음
- `install-eks.sh:490-498`: 나중에 IMAGE_KEYS 에 키를 추가하면서 case 를 빠뜨리면 `repo="llm-gateway/"` 가 된다. 그러면 AWS 가 ImageNotFound 가 아니라 검증 오류를 내고, 스크립트는 "조회 실패 — 건너뜀" 경고만 남긴 채 preflight 를 통과한다(fail-open).
- 최소 수정: `*) echo "BUG: ecr_repo 미등록 키 $1" >&2; return 1 ;;`. 이렇게 하면 `set -e` 상태에서 `repo=$(...)` 할당이 실패해 즉시 중단된다.

### [N] SW-2 — automount 판정과 빈 문자열
- `notification-worker/serviceaccount.yaml:17`: `role-arn: ""` 이어도 `hasKey` 는 true 를 돌려준다(실측: `automountServiceAccountToken: true`). 영향은 미미하다. 참고로 EKS IRSA 웹훅은 별도 projected 토큰(`aws-iam-token`)을 주입하므로 IRSA 는 SA 토큰 automount 와 무관하게 동작한다. 그래서 `false` 로 고정해도 SES 발송은 깨지지 않는다(웹훅 동작 기준, 이 클러스터에서는 실측하지 않음).
- 최소 수정(선택): `{{ ne (get (.Values.notificationWorker.serviceAccount.annotations | default dict) "eks.amazonaws.com/role-arn" | default "") "" }}`

---

## 결론

라운드 3 처리분 중 N-15·N-17 은 해결됐고, S-6(install-eks)·S-7(원래 오탐)·N-8(env 전달)·N-16(3개 서비스)도 실행으로 확인했다.
수렴을 막는 항목은 **S-6r4** 하나다. 라운드 3 에서 install-eks.sh 의 연관 배열을 고쳤지만, 같은 결함이 R24 가 안내하는 배포 경로인 `rebuild-image.sh:37` 에 남아 있다. bash 3.2(macOS 기본)에서 rebuild-image 를 실행하면 첫 실행부터 즉시 종료된다. 나머지는 모두 fail-closed 이거나 테스트 커버리지 갭인 N 등급이다.

VERDICT: NOT CONVERGED
