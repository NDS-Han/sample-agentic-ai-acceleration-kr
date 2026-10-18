# R24 리뷰 라운드 5 — 수렴 판정

대상: 라운드 4 처리분(S-6r4, S-7r4, N-8r4, N-16r4, SW-1, SW-2)과 `/tmp/r24-round4.diff` 누적 스윕.
방법: 처리 내역의 주장은 근거로 쓰지 않았고, 모든 판정은 실행해서 확인했다.
- bash 3.2: `bash:3.2`(3.2.57) 이미지에 git·jq 를 추가한 컨테이너를 썼다. aws/helm/kubectl/docker 는 스텁으로 대체했다.
- 시나리오용으로 임시 git 리포를 만들었다(c1→c2[db]→c2b[app], 분기 c1→c3[app]→c4[db], feat(db X) vs main2(같은 db X 를 squash)).
- 실행 대상: `rebuild-image.sh` 는 스크립트 전체를, `install-eks.sh` 는 `helm_install` 함수만 추출해 끝까지 실행했다.
- 차트 변이(mutation)는 원본을 백업한 뒤 주입했고, 끝난 뒤 `diff -r` 로 원복을 확인했다.

## 요약

| ID | 판정 | 근거 |
|---|---|---|
| S-6r4 | ✅ 해결 | bash 3.2 에서 6개 서비스 모두 rc=0, 키 매핑 정확. bogus 서비스는 rc=1 과 안내 메시지 |
| S-7r4 | ✅ 해결 | three-dot 게이트 6개 시나리오 × 2개 스크립트 모두 기대와 일치 |
| N-8r4 | ✅ 해결 | init 실패 시 rc=1, 성공·state 없음이면 스킵(rc=0). env 주입 테스트 존재 |
| N-16r4 | ✅ 해결 | 5개 서비스 각각 변이를 넣으면 해당 파라미터가 1 failed. 양성 대조 존재 |
| SW-1 | ✅ 해결 | 미등록 키면 bash 3.2/5 모두 `set -e` 로 즉시 중단 |
| SW-2 | ✅ 해결 | `role-arn: ""` → false, ARN → true, 어노테이션 없음/무관 키 → false |
| 신규 M/S | 없음 | — |
| 신규 N | 1건 (fail-closed 과차단, 아래 N-1r5) | — |

기준선: `pytest deployment/tui/tests deployment/charts/llm-gateway/tests` → **149 passed**.
helm lint/template 은 4개 values 파일 모두 ok(helm v3.21.4). `deployment/scripts/*.sh` 11개 전부 bash 3.2 `-n` OK.

---

## 1. 라운드 4 잔여 검증

### S-6r4 — rebuild-image.sh `values_key()` (bash 3.2 실행)

```
gateway-proxy        rc=0 :: --set gatewayProxy.image.tag=<sha>
admin-api            rc=0 :: --set adminApi.image.tag=<sha> --set scheduler.image.tag=<sha>
admin-ui             rc=0 :: --set adminUi.image.tag=<sha>
notification-worker  rc=0 :: --set notificationWorker.image.tag=<sha>
cost-recorder-worker rc=0 :: --set costRecorderWorker.image.tag=<sha>
migration            rc=0 :: --set migration.image.tag=<sha>
bogus                rc=1 :: ❌ 알 수 없는 서비스: bogus (가능: …)
```
`DEPLOY=1` 경로도 bash 3.2 에서 helm 버전 preflight → ownerRef 루프 → `helm upgrade` 호출까지 진행됐다(아래 게이트 표의 PASS 행).
`KEY="$(values_key …)" || {…}` 는 `*) return 1` 이 반환하는 실패 코드를 정확히 받는다.

### S-7r4 — three-dot 게이트 (bash 3.2, 실제 스크립트 실행)

| 시나리오 | mig | app | install-eks | rebuild-image | 기대 |
|---|---|---|---|---|---|
| (a) mig>app | c2 | c1 | PASS | PASS | PASS ✓ |
| (b) app>mig + db | c1 | c2 | BLOCK | BLOCK | BLOCK ✓ |
| (b') app>mig, db 무변경 | c2 | c2b | PASS | PASS | PASS ✓ |
| (c1) 분기, 앱 쪽 db | c2 | c4 | BLOCK | BLOCK | BLOCK ✓ |
| (c2) 분기, mig 쪽 db 만 | c2 | c3 | PASS | PASS | PASS ✓ (r4 과차단 해소) |
| (d) mig=app | c2 | c2 | PASS | PASS | PASS ✓ |
| (f) mig 태그가 SHA 아님 | 1.0.54-us19 | c2 | — | PASS(스킵) | 설계상 스킵 ✓ |

IMAGE_TAG 경로도 bash 3.2 에서 함께 확인했다. 7개 ECR 리포에 태그가 모두 있으면 PASS 하고, ImageNotFound 이면 누락된 7개 `repo:tag` 를 출력하고 rc=1 로 끝난다.

### N-8r4 — teardown

- 스텁 terraform 으로 `_HELM_UNINSTALL` 원문을 실행했다.
  - `INIT_FAIL=1`: "ERROR: terraform init 실패…", rc=1
  - init 이 성공하고 output 이 `{}`: "cluster_name output 없음 — helm 단계 스킵", rc=0. 정상 스킵 경로가 회귀하지 않았다.
- init 인자는 `-reconfigure -backend-config=bucket=b -backend-config=dynamodb_table=t` 로 정확히 전달된다. `backend_args()` 값에는 공백이 없어서 `$TF_BACKEND_ARGS` 단어 분리가 안전하다.
- `test_steps.py:115-125` 가 `helm.env["TF_BACKEND_ARGS"]` 와 init 이 output 보다 먼저 오는 순서를 assert 한다. 149 passed 에 포함돼 있다.

### N-16r4 — PDB 변이 주입

5개 `pdb.yaml` 마다 ternary 를 `gt replicaCount minAvail` 로 되돌리는 변이를 넣었다.
```
mutate admin-api / admin-ui / gateway-proxy / notification-worker / cost-recorder-worker
  → 각각 "1 failed, 6 passed"  (해당 서비스 파라미터만 실패)
```
prod 의 replicaCount(3/3/2/2/3)는 모두 minAvailable=1 보다 크다. 그래서 replicaCount 를 고정하지 않아도 변이를 잡는다. 양성 대조(`test_pdb_rendered_when_hpa_min_gt_min_available`)는 adminApi 1개만 덮는다. 이 공백은 "PDB 가 사라지는" 방향이라 drain 을 막지 않으므로 운영 중단과는 무관하다. 생략한다.

### SW-1 / SW-2

- SW-1: `repo="pre/$(g zz)"` 형태(복합 문자열 할당 안의 명령 치환이 실패하는 경우)를 시험했다. bash 3.2 와 bash 5 모두 `set -e` 로 rc=1 이 되고 다음 줄로 진행하지 않았다. `helm_install` 은 `main` 에서 조건문 밖에서 호출되므로 `set -e` 가 유효하다.
- SW-2 렌더 결과(prod values):

| annotations | automount |
|---|---|
| 없음 | false |
| `role-arn: ""` | false |
| `role-arn: arn:…` | true |
| `foo: bar` | false |

---

## 2. 누적 diff 잔여 스윕

| 항목 | 결과 |
|---|---|
| bash4 전용 구문 (`declare -A/-n`, `mapfile`, `readarray`, `${x,,}`, `${x^^}`, `${x@Q}`, `\|&`, `;;&`, `coproc`, `wait -n`, `[[ -v`) | install-eks / rebuild-image / smoke-test / steps.py 에서 0건(주석 2건 제외) |
| `${out%%$'\n'*}`, 빈 배열 `${#m[@]}` (`set -u`, 3.2) | `[line1]`, `len=0`. 정상 |
| 빈 배열 `"${a[@]}"` (`set -u`, 3.2) | `missing` 은 `${#missing[@]} -gt 0` 가드 안에서만 확장한다. `SET_ARGS`·`IMAGE_KEYS`·`HELM_CMD` 는 항상 채워져 있다 |
| `local` 미선언 변수의 전역 오염 | `helm_install` 의 `live_values key live_tag missing repo out mig_tag app_tag IMAGE_KEYS HELM_MAJOR STS_REGIONS_CSV` 가 전역이 된다. 이후 함수(`patch_nextauth_url`, `verify_deployment`)가 읽는 전역은 `SET_ARGS`·`ROLLBACK_FLAG` 뿐이고, 둘 다 의도된 공유다. `missing` 은 앞선 함수들에서 `local` 이다. 같은 이름을 다른 뜻으로 쓰는 곳이 없어 동작 영향은 없다 |
| rebuild-image `set -- $HELM_MAJOR` 이후 `$1/$2` | SVC/ENV 를 이미 27-28행에서 캡처했다. 영향 없음 |
| three-dot 경계: 같은 내용, 다른 SHA (squash/rebase) | **과차단(fail-closed)**. 아래 N-1r5 |
| three-dot 경계: merge-base 없음 (shallow/orphan) | `fatal: no merge base` 로 rc=128 → `!` 때문에 BLOCK. 이때 "db/ 변경이 있다" 는 오진 메시지가 나온다. 아래 N-1r5 |
| 우회·교착 여부 | 과차단돼도 `rebuild-image.sh migration`(게이트 없음) 또는 `IMAGE_TAG=<sha>`(mig=app 이라 스킵)로 즉시 풀린다. 교착 없음 |

### [N] N-1r5 — three-dot 게이트가 이력(merge-base)에 의존해서 내용이 같아도 차단한다

운영 판단: 이 결함은 fail-closed 이고 우회 경로가 있다. 그래서 운영을 멈추게 하지는 않는다. 다만 squash-merge 워크플로우나 CI shallow clone 에서는 거짓 차단이 반복될 수 있어 기록한다. **수렴을 막지 않는다.**

- 파일:라인: `deployment/scripts/install-eks.sh:573`, `deployment/scripts/rebuild-image.sh:101`
- 재현 (bash 3.2, 실측):
  - feature 브랜치 커밋 M(db X 추가)으로 migration 을 배포했다. 이후 main 에 같은 X 를 squash 한 S 를 만들었다. `git rev-parse M:db == S:db` 로 db 트리가 동일함을 확인했다. 그런데도 S 에서 gateway-proxy 를 배포하면 **BLOCK** 된다(두 스크립트 모두).
  - `git clone --depth=1 --no-single-branch` 리포에서 mig=c2b, app=c3 이면 `no merge base` 로 rc=128 이 되어 BLOCK 된다. 기대 결과는 PASS 다.
- 최소 수정(선택): merge-base 대신 내용을 비교한다. 즉 "앱에는 있는데 mig 에는 없는 revision 파일" 만 본다.
  ```bash
  # install-eks.sh:573
  && ! git -C "$DEPLOY_DIR/.." diff --quiet --diff-filter=A "$mig_tag" "$app_tag" -- db/versions/ 2>/dev/null; then
  # rebuild-image.sh:101
  && ! git -C "$ROOT" diff --quiet --diff-filter=A "$live_mtag" HEAD -- db/versions/; then
  ```
  위 시나리오 전부를 실측했다: (a) PASS, (b) BLOCK, (b') PASS, (c1) BLOCK, (c2) PASS, (d) PASS, squash PASS, shallow PASS. 기대와 모두 일치한다.
  이 방식은 기존 revision 파일의 *수정*(M)은 보지 않는다. 적용된 alembic revision 을 고치는 것은 그 자체로 금지 관행이므로 받아들일 만한 트레이드오프다.

### 범위 밖 관찰 (비차단, 이전 라운드에서 수용된 설계)

- `rebuild-image.sh` 기본 경로(`DEPLOY=0`)는 helm upgrade 명령만 출력하고 migration 게이트를 거치지 않는다. 운영자가 출력된 명령을 그대로 실행하면 "새 앱 + 구 스키마" 조합이 가능하다. 라운드 1~2 에서 S5 가 DEPLOY=1 범위로 수용됐으므로 R24 수렴 대상은 아니다. 후속 라운드에서 DEPLOY=0 출력 앞에 같은 게이트를 warning 으로 붙이는 것을 권한다.

---

## 결론

라운드 4 잔여 6건(S-6r4, S-7r4, N-8r4, N-16r4, SW-1, SW-2)은 모두 bash 3.2 실행, 스텁 하네스, 변이 주입, 렌더로 해결을 확인했다.
누적 diff 스윕에서 새 M/S 급은 없었다. 남은 것은 fail-closed 과차단인 N-1r5 하나이고, 우회 경로가 확인됐으며 수정안도 하네스로 검증했다.

VERDICT: CONVERGED
