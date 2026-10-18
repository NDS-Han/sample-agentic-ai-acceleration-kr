# R24 — deployment 표면 리뷰 라운드 3 (수정 검증, kiro/opus)

대상: 라운드 2 처리 내역(S-1~S-5, N-1~N-14)과 `/tmp/r24-round2.diff`. 처리 내역의 주장은 검증 근거로 쓰지 않았다. 판정은 실행 결과로만 했다.

## 실행한 검증

| 항목 | 방법 | 결과 |
|---|---|---|
| install-eks `helm_install`/`patch_nextauth_url` | 함수를 원본에서 그대로 추출해 하네스로 실행했다. helm/aws/kubectl은 stub, git은 실제 저장소를 썼다 | 아래 S-1/S-2/N-2~N-4/N-11 행 참조 |
| 같은 하네스, bash 3.2 / 5.2 | `docker run bash:3.2`, `bash:5.2` | **3.2에서 `declare -A` 때문에 중단됨(S-6)**. 5.2는 정상 |
| rebuild-image.sh 전체 | aws/docker/kubectl/helm을 stub으로 두고 DEPLOY=1로 실행. helm 버전 6종 | 아래 N-6/N-13 행 참조 |
| `helm template` | base / dev / prod / prod+loadtest에 per-ingress override, PDB 조합 25개를 더해 렌더 | 아래 S-3/S-4/N-1 행 참조 |
| `helm lint` 4조합, `bash -n`, shellcheck(`koalaman/shellcheck:stable -S warning`) | | 전부 통과. shellcheck 경고 0 |
| terraform validate (dev / prod / gateway-ecs, `-backend=false -lockfile=readonly`), `fmt -check -recursive` | | 3개 모두 valid, fmt clean |
| teardown `_HELM_UNINSTALL` | `steps.py`에서 문자열을 그대로 꺼내 stub 환경 8가지로 실행 + 실제 미초기화 tf 디렉터리 | N-8 잔여 |
| 라이브 dev (읽기 전용) | `kubectl get secret/externalsecret` | Secret 3개 owner=ExternalSecret, ES 3개 Owner/1h. 루프 대상 이름과 일치 |
| pytest | chart 42(safety 15), TUI 101, NDS 111 | 전부 통과 |
| 변이 검사 | 차트 사본 + main.py 사본으로 9개 변이 | 7개 FAIL(검출), 2개 PASS(미검출) → N-16 |

## 항목별 판정

| ID | 판정 | 근거(실측) |
|---|---|---|
| S-1 | ✅ | T1b `install IMAGE_TAG=c75d1e92` → SET_ARGS에 7개 키 전부 `=c75d1e92`. T1a(IMAGE_TAG 없음) → 0개. T2 `helm get values` 실패 → `E: helm get values 실패…` 후 종료(helm upgrade 미호출) |
| S-2 | ⚠️ 롤백·values-only는 해소됨. 방향성이 없어 역방향 오탐이 생김(S-7) | 롤백 `IMAGE_TAG=dd081b50`(라이브 c75) → mig=app → 통과. values-only(라이브 7개 dd081, 이후 db 커밋 있음) → 통과. mig<app + db 변경(dd081→c75, eed423d6이 db/ 변경) → 중단(정탐). mig<app + db 변경 없음(eed4→c75) → 통과. **mig>app + db 변경(mig c75, app dd081) → 중단(오탐)** |
| `values_tag` | ✅ 현재 파일 기준 | dev/prod 7개 키 모두 정확(`1.0.87-us19`, `1.0.54-us19` 등). values.yaml의 인라인 주석이 붙은 줄도 정확. 블록 안에서 `tag:`가 들어간 줄은 각 1개뿐이라 오매칭 없음. 취약점은 N-17 |
| S-3 | ✅ | prod → `https://admin.CHANGE_ME.example.com` / `true`. prod + `adminUi listen-ports=[{"HTTP":80}]` → `http://admin.CHANGE_ME…` / `false`(Ingress도 `[{"HTTP":80}]`로 렌더). dev → `http://` / `false`. dev + HTTPS override → `https://` / `true`. loadtest = prod. `patch_nextauth_url`: 렌더가 `https://`면 `https://<ALB>`, `http://`면 `http://<ALB>`. prod는 호스트가 확정돼 있어 스킵 |
| S-4 | ✅ | prod·loadtest gateway Ingress에만 `deregistration_delay.timeout_seconds=600`. 600 ≥ 15 + 570 = 585 |
| S-5 | ✅ | `main.tf:48` command에 `--timeout-graceful-shutdown $${UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:-15}`. `svc_command`가 `sh -c`로 감싸고, sh에서 확장한 결과는 `15`. 이미지·taskdef 어디에도 `UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT` ENV가 없어 이미지 기본 240이 끼어들 경로가 없다. 60 = 15 + 40 + 5. validate 통과 |
| N-1 | ✅ | 5개 서비스 × 5조합(minAvailable=1): HPA min=1 + replica 3 → 미렌더. HPA min=2 + replica 1 → 렌더. 정적 1 → 미렌더, 정적 2 → 렌더. prod(minAvailable=2)에서 HPA min=2 → 미렌더(2>2 거짓, 정상) |
| N-2 | ✅ | `${live_values:-{}}` 0건. `live_values='{}'`로 초기화한 뒤 herestring 사용 |
| N-3 | ✅ | migration 키 누락 → `W: 라이브 values 에 migration.image.tag 없음…`. 나머지 6개는 라이브 태그로 보존 |
| N-4 | ✅ | admin-ui ImageNotFound → 누락 목록 출력 후 종료(rc=1). migration AccessDenied → 경고 후 진행. 7회 조회 중 admin-api는 scheduler 몫까지 2회(무해). 리포지토리 맵은 values.yaml `repository`와 일치 |
| N-6 | ✅ | stub에서 owner=ExternalSecret인 app/redis만 patch, db(owner 없음)는 건너뜀. 이름은 렌더된 ES와 라이브 Secret과 일치. ES가 pre-upgrade hook(-30, before-hook-creation)이라 patch가 hook 삭제보다 먼저 실행된다 |
| N-8 | ⚠️ 5/6 | state 비어 있음(`{}`) → 스킵. ns NotFound → 스킵. Unauthorized → rc=1. helm `not found` → 계속. helm unreachable → rc=1. Ingress 잔존 → wait. **미초기화 디렉터리 → 스킵(rc=0)**: 주장과 다르다. 아래 N-8 잔여 참조 |
| N-9 | ✅ | `"DevRole"` → `['DevRole']`, 렌더 env `DevRole`. 리스트는 그대로. 테스트 2건 통과 |
| N-11 | ✅ | `DEBUG_MODE=true`면 patch upgrade 인자에 `--atomic`/`--cleanup-on-fail`이 없다. false면 둘 다 있다 |
| N-13 | ✅ (fail-open 1건, N-15) | sed 파싱 결과: v3.14.0 → `3 14` 통과, v3.13.3 → 중단, v3.21.4·v4.0.1 → 통과, helm 실행 실패 → `확인 불가` 중단. 중단은 docker build/push 전에 일어난다. 실패 시 HELM_CMD를 출력한다 |
| N-14 | ⚠️ | 9개 변이 중 7개 검출, 2개 미검출(N-16) |
| `set -- $HELM_MAJOR` | ✅ 회귀 없음 | rebuild-image.sh에서 `$1`/`$2`는 28-29행 `SVC`/`ENV` 대입에만 쓰이고 108행 이후 참조가 없다(grep). stub 실행에서도 `rollout status deploy/llm-gateway-gateway-proxy`, `helm set: gatewayProxy…`가 정상 |
| `IMAGE_KEYS`/`ECR_REPO` 스코프 | ✅ bash ≥4 | `declare -A`는 함수 안에서 local이고, 사용처가 `helm_install` 안에만 있다. `IMAGE_KEYS`는 전역이지만 충돌하는 이름이 없다. bash 5.2 + `set -u`에서 정상. **bash 3.2에서는 중단(S-6)** |

---

## 잔여/신규 발견

### Should fix (S)

**S-6. install-eks.sh: `declare -A`(bash ≥4 전용) 추가로 macOS 기본 bash 3.2에서 모든 실행이 중단됨 — 이번 라운드에서 생긴 회귀** (`deployment/scripts/install-eks.sh:489`)
- HEAD의 install-eks.sh에는 bash 4 전용 기능이 0건이다(grep: `declare -A`/`mapfile`/`declare -n`/`${,,}`).
  `01-prerequisites.md §2.2`는 macOS(brew) 배포자를 공식 경로로 안내하고, bash 버전 요구는 어디에도 없다. `#!/usr/bin/env bash`는 macOS에서 `/bin/bash` 3.2로 해석된다.
- 재현: `docker run bash:3.2 bash run32.sh install` → `fn_helm_install.sh: line 114: gatewayProxy: unbound variable`로 종료된다(bash 3.2는 `-A`를 인덱스 배열로 처리하고, `[gatewayProxy]`를 산술식으로 평가하다 `set -u`에 걸린다).
  같은 하네스를 bash 5.2로 돌리면 정상이다. 중단 시점은 namespace·ClusterSecretStore·ES 정리 같은 부작용이 끝난 뒤, helm 실행 직전이다. IMAGE_TAG 여부와 관계없이 매번 중단된다.
- 최소 수정: 연관 배열 대신 함수를 쓴다.
  ```bash
  ecr_repo() { case "$1" in
      gatewayProxy) echo gateway-proxy;; adminApi|scheduler) echo admin-api;;
      adminUi) echo admin-ui;; notificationWorker) echo notification-worker;;
      costRecorderWorker) echo cost-recorder-worker;; migration) echo migration;; esac; }
  # repo="llm-gateway/$(ecr_repo "$key")"
  ```
  (rebuild-image.sh 37행의 `declare -A VALUES_KEY`는 HEAD부터 있던 것이라 회귀는 아니다. 같은 이유로 macOS에서 깨지므로 함께 바꾸거나 prerequisites에 "bash ≥4 (brew install bash)"를 명시한다.)

**S-7. S-2 게이트가 방향을 보지 않아, "migration 먼저" 순서로 배포한 중간 상태에서 install-eks가 막힘** (`install-eks.sh:556-563`)
- `git diff --quiet mig app -- db/`는 대칭이다. migration이 앱보다 **앞서** 있는 상태도 차단한다. 이 상태는 정상적인 expand 단계이고, rebuild-image의 권장 순서(migration 먼저, 그다음 앱) 사이에 늘 생긴다. 구 앱 + 새 스키마이므로 위험한 조합도 아니다.
- 재현(하네스): 라이브 `migration=c75d1e92`, `gatewayProxy·나머지=dd081b50`(그 사이 eed423d6이 db/ 변경), IMAGE_TAG 없이 upgrade → `E: migration 태그(c75d1e92)와 앱 태그(dd081b50) 사이에 db/ 변경이 있다`로 중단된다.
  에러 메시지의 처방("migration 이미지부터 배포")은 이미 끝난 상태라 무한 루프가 된다. 앱을 모두 올리기 전까지 values-only 변경(시크릿·어노테이션·NEXTAUTH 등)이 불가능하다. 라운드 2 S-2가 지적한 오탐이 방향만 바뀐 채 남았다.
- 최소 수정(하네스로 검증함): 앱 태그가 migration 태그의 조상이면 통과시킨다.
  ```bash
  && ! git -C "$DEPLOY_DIR/.." merge-base --is-ancestor "$app_tag" "$mig_tag" 2>/dev/null \
  && ! git -C "$DEPLOY_DIR/.." diff --quiet "$mig_tag" "$app_tag" -- db/ 2>/dev/null; then
  ```
  패치 후 결과: mig<app + db → 중단(유지), mig>app + db → 통과, mig<app + db 변경 없음 → 통과.
  보강할 점: `app_tag`가 `gatewayProxy`만 본다. 스키마의 주 소비자는 adminApi/scheduler/costRecorderWorker이므로, 각 앱 키에 대해 같은 검사를 루프로 돌리는 편이 정확하다(지금은 rebuild-image 게이트가 막아 주므로 잠복 상태).

### Nice to have (N)

| ID | 발견 | 위치 | 재현/근거 | 최소 수정 |
|---|---|---|---|---|
| N-8 잔여 | 처리 내역은 "미init 디렉터리 오인 방지"라고 했지만, 미초기화 디렉터리에서는 여전히 "state 없음 → 스킵(exit 0)"이다. `build_llm_teardown`의 단계 순서가 `helm-uninstall` → `tf-init`이라, 새 체크아웃이나 다른 머신에서 teardown하면 ALB를 남긴 채 destroy가 진행된다(VPC DependencyViolation) | `deployment/tui/steps.py:90-92, 138-145` | `.tf`만 복사한 S3 backend 디렉터리에서 `terraform output -json` → `Backend initialization required`(rc=1) → gate rc=4 → stub 실행 `[TF=uninit] rc=0 "…helm 단계 스킵"` | `tf-init` Step을 `helm-uninstall` 앞으로 옮긴다. 스크립트에서는 `terraform output` 실패를 exit 1로 처리하고, `{}`(성공 + 빈 출력)일 때만 스킵한다 |
| N-15 | helm 버전 preflight가 숫자가 아닌 출력에서 fail-open | `rebuild-image.sh:106-112` | `HELM_VER='version.BuildInfo{Version:"v3.10.0"}'`, `'<no value>'` → `[: integer expected` 뒤 if가 거짓 → 빌드/푸시 진행(rc=0) | `[[ "$HELM_MAJOR" =~ ^[0-9]+\ [0-9]+$ ]] \|\| { echo "❌ helm 버전 파싱 불가"; exit 1; }` |
| N-16 | 신규 테스트 공백 2건. ① `test_pdb_rendered_when_replicas_or_hpa_min_gt1`은 `"gateway" in n`으로 검사하는데, 릴리스 이름 때문에 모든 PDB 이름에 `gateway`가 들어 있어 사실상 아무것도 검사하지 않는다. ② HPA 가드 테스트가 admin-api만 덮고, 나머지 PDB 4개는 가드를 되돌려도 검출되지 않는다. `_ingress(docs, "gateway")`도 같은 부분 문자열 문제로 렌더 순서에 의존한다 | `charts/llm-gateway/tests/test_deployment_safety.py:173-177, 199-210, 216-220, 253` | 변이 M4(prod `gatewayProxy.podDisruptionBudget.enabled=false`) → 15 passed. M1b(gateway-proxy PDB 가드를 `gt replicaCount`로 되돌림) → 15 passed. 검출된 변이: admin-api 가드, helper merge 순서 반전, deregistration 제거와 500, NEXTAUTH scheme을 tls.enabled로 되돌림, main.py lifespan 80, loadtest grace 60 | 이름 비교를 `n.endswith("-gateway-proxy")` 같은 정확 접미사로 바꾸고, `_ingress`는 `endswith("-gateway")`로 바꾼다. HPA 가드 케이스를 5개 서비스로 parametrize한다 |
| N-17 | `values_tag` awk는 블록 안에서 처음 나오는 `tag:` 줄을 집는다. 들여쓴 주석(`# tag: …`)이나 flow 스타일(`image: {tag: x}`)이 들어오면 엉뚱한 토큰을 반환한다. 다만 그 값은 `rev-parse` 실패로 게이트를 건너뛰는 쪽(fail-open)이라 피해는 작다. 별개로, README의 "`# CHANGE_ME` 마커 dev 10 / prod 16"은 `CHANGE_ME`가 들어간 **줄** 수이고, `# CHANGE_ME` 주석 마커는 9/9개다. 새로 바꾼 prod host 3개에는 주석 마커가 없다 | `install-eks.sh:552-555`, `README.md:217` | 현재 dev/prod/values.yaml에서는 블록당 `tag:` 줄이 1개라 정확(실측) | 정규식을 `/^[[:space:]]+tag:/`로 좁히고 주석 줄은 제외한다. README 문구를 "`CHANGE_ME` 문자열"로 바꾼다 |

### 범위 밖 관찰
- `adminUiHttps`는 `tls.enabled`와 `listen-ports`만 본다. AWS LB Controller는 `certificate-arn`만 있고 `listen-ports`가 없으면 기본값으로 HTTPS:443을 쓰는데, 이 조합은 `false`로 판정된다. 현재 values는 모두 listen-ports를 명시하므로 잠복 상태다.

Not checked: macOS 실기(대신 bash 3.2 컨테이너로 재현), prod 클러스터·ECR 실물(이 계정에는 ECR 조회 권한 없음), ESO가 ownerRef 제거 직후 재부착하는지(라이브에서 patch 미실행 — 쓰기 작업이라 생략), 실제 ALB deregistration 타이밍, ECS 실배포.

Verdict: S-1, S-3, S-4, S-5, N-1~N-4, N-6, N-9, N-11, N-13은 실행으로 확인했다. 남은 것은 두 가지다. 하나는 이번 라운드에서 생긴 회귀 S-6(bash 3.2에서 install-eks 전면 중단)이고, 다른 하나는 S-2 수정이 대칭 비교라 생긴 역방향 오탐 S-7이다. 둘 다 수정이 몇 줄이고, S-7 수정은 하네스로 검증했다. N-8 잔여는 처리 주장과 실제가 다르므로 함께 고치는 것을 권한다.

VERDICT: NOT CONVERGED
