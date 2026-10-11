# upstream 동기화 — pr/nds-delta → nds/dev 머지 절차

두 갈래 작업이 동시에 진행됩니다:

- **`pr/nds-delta`** — upstream(aws-samples fork)에 올리는 PR 작업 브랜치.
  maintainer 리뷰에 따라 재작성(force-push)된다.
- **`nds/dev`** — NDS 서비스 개선 브랜치. upstream에 올리지 않는 NDS 고유
  기능이 들어 있다.

목표: upstream에 올린 변경이 `nds/dev`에도 계속 반영되게 한다.
이 문서는 그 머지를 반복 가능한 절차로 고정합니다.

## 배경 — 왜 수동 절차가 필요한가

`pr/nds-delta`는 리뷰 라운드마다 **같은 주제를 재작성한 커밋으로 force-push**
됩니다(관측: `b428369` → `56f16e48` → `3d653c82`, merge-base는 `ccabd6a`
고정). git은 재작성된 커밋을 새 커밋으로 보기 때문에, 지난번에 해결한 충돌과
같은 충돌이 매번 다시 발생합니다. 게다가:

- **자동머지가 조용히 결함을 만든다** — 충돌로 표시되지 않는 병합이
  함수 정의 중복·구버전 덮어쓰기·회귀를 일으킨다(실제 발생:
  `rate_limits.py` 라우트 3중, `19-admin-login.sh` 헬퍼 2중,
  `LOGIN_PATH` 회귀).
- **NDS 고유 코드와 upstream 코드가 같은 파일에 섞여 있다** — 파일 단위
  `checkout --theirs`를 하면 NDS 수정이 통째로 날아간다(실제 발생:
  `messages.py`의 1h 캐시 과금 픽스·`anthropic_error` 스키마).

## git rerere — 반복 충돌 해결을 기억하게 하기

`rerere`(**re**use **re**corded **re**solution)는 충돌 해결 내용을 git이
기억했다가, 같은 충돌이 다시 나오면 자동으로 재적용하는 기능입니다.
force-push된 PR 브랜치와의 머지처럼 "같은 충돌이 반복되는" 워크플로우에
정확히 맞습니다.

```bash
# 한 번만 설정 (로컬 저장소 설정 — 원격에 영향 없음)
git config rerere.enabled true
git config rerere.autoupdate true   # 재적용된 해결을 자동으로 stage까지
```

동작 방식:

1. 머지 중 충돌이 나면 git이 충돌 hunk의 "지문"과 우리가 고른 해결을
   `.git/rr-cache/`에 기록한다.
2. 다음 머지에서 **같은 지문의 충돌**이 나오면 기록된 해결을 자동 적용한다
   (`autoupdate`면 stage까지). 해결이 필요 없는 충돌은 사람이 안 본다.
3. 다르게 해결하고 싶으면 평소처럼 고치면 되고, 새 해결이 기록을 갱신한다.

주의:

- **로컬 저장소 설정**이라 팀원 각각 켜야 하고, 클론을 새로 하면
  rr-cache도 새로 쌓입니다.
- 자동 재적용은 hunk 지문이 완전히 같을 때만 — 주변 코드가 바뀌어 지문이
  달라지면 다시 수동입니다. 그래도 지금처럼 merge-base가 고정된 재작성
  머지에서는 대부분 재적용됩니다.
- rerere를 켜도 **아래 검증 단계(자동머지 함정 검사)는 생략 불가** — rerere는
  "충돌로 표시된" 것만 다루고, 조용히 잘못 병합되는 건 못 잡습니다.

## 표준 머지 절차

저장소 루트(`sample-agentic-ai-acceleration-kr`)에서:

```bash
# 0. 작업 트리가 깨끗한지 확인
git status --short

# 1. 최신 PR head 가져오기
git fetch origin pr/nds-delta

# 2. 이전에 머지한 head와 새 head 비교 — 델타 파악
#    (이전 head는 지난 머지 커밋 메시지에 기록돼 있다)
git log --oneline origin/pr/nds-delta --not <merge-base>
git diff --stat <이전-head>..origin/pr/nds-delta   # 재작성 전후 순수 델타

# 3. nds/dev에 머지 시작
git merge origin/pr/nds-delta
```

### 3-a. 충돌 해결 원칙

파일 전체 `--theirs`/`--ours`는 **마지막 수단**입니다. 기본은 hunk 단위:

| 방향 | 기준 | 현재 목록 (2026-10 기준) |
|---|---|---|
| **theirs** | upstream과 동일하게 유지해 향후 머지를 쉽게 — 재제출본의 리뷰 반영·신규 기능·운영 문서 | `thinking_normalizer.py`, `gateway-proxy/tests/**`, `docs/us-llm-gateway/**`, US-19 신규 파일, `db` 스키마/마이그레이션 |
| **ours** | NDS 고유 기능 — upstream에 없고 올리면 안 되는 것 | `roleFromCookie`/`ADMIN_ROLE_COOKIE` (layout.tsx), `revoke_keys_for_users` (key_service.py), budget `cap_source`·팀 기본 cap·다운그레이드 배지, `tracked` 플래그, `UsageTrendChart` ReportingTZ, `CHAT_ENABLED` 게이트, `NDS/` 문서·`deploy` 스크립트, `provider: mock` 기본값 |
| **혼합** | 양쪽에 다른 수정이 공존 | `messages.py` — NDS 과금/에러 스키마는 ours + PR 정규화 주석/필드는 theirs |

판단 기준: "이 hunk가 upstream에도 존재하는 코드인가?" — 그러면 theirs가
미래 머지를 쉽게 하고, NDS 전용이면 ours입니다.

### 3-b. 자동머지 함정 검사 (필수 — 충돌 없이 지나간 파일도 검사)

충돌이 없었다고 안전한 게 아닙니다. 매번 실행:

```bash
# 충돌 마커 잔존 — 0건이어야 함
git grep -nE '^(<{7}|={7}|>{7}) ' || echo "markers: 0"

# 중복 함수/라우트 정의 — 자동머지가 양쪽 정의를 둘 다 살리는 대표 함정.
# 같은 이름의 def/route decorator가 한 파일에 2번 이상이면 확인
cd projects/awsome-ai-gateway
for f in $(git diff --name-only HEAD@{1}..HEAD -- '*.py' 2>/dev/null); do
  dups=$(grep -cE '^(async )?def [a-z_]+|^@(router|app)\.' "$f" 2>/dev/null)
  awk '/^(async )?def /{print FILENAME": "$2}' "$f"
done | sort | uniq -d
# → 동일 "파일: 함수명"이 2줄 이상이면 중복 정의 의심

# bash 문법 — 머지된 스크립트 전부
git diff --name-only HEAD@{1}..HEAD -- '*.sh' | xargs -r -n1 bash -n

# 실행비트 — PR 브랜치에서 100755→100644로 내려간 스크립트가 없는지
git diff HEAD@{1}..HEAD --summary | grep 'mode change'
```

실제로 잡힌 사례(이 절차를 태운 이유):

- `rate_limits.py` `usage-trend` 라우트 — 우리 쪽 2중 + 머지가 3번째 추가 → 1개로 정리
- `19-admin-login.sh` `tf_var_logouts` 헬퍼 2중 — 구버전이 신버전을 덮어씀
- `20-enable-body-logging.sh` 실행비트 상실(100755→100644)
- `LOGIN_PATH` 회귀 — 머지가 our쪽 `/login`을 채택해 PR의 `/api/auth/login` 원복이 유실 (로그인 불가 상태)

### 4. 검증 배터리

```bash
cd projects/awsome-ai-gateway

# Python 구문 — 머지로 바뀐 파일 전부
git diff --name-only HEAD@{1}..HEAD -- '*.py' | xargs -r -n1 python3 -m py_compile

# YAML — helm values 등
git diff --name-only HEAD@{1}..HEAD -- '*.yaml' '*.yml' | \
  xargs -r -n1 python3 -c "import yaml,sys; yaml.safe_load(open(sys.argv[1]))"

# 테스트 — 각 프로젝트 가상환경 (시스템 python에는 pytest 없음)
gateway-proxy/.venv/bin/pytest tests -x -q          # ~1200개
admin-api/.venv/bin/pytest tests -x -q              # ~850개
cost-recorder-worker/.venv/bin/pytest tests -x -q
notification-worker/.venv/bin/pytest tests -x -q    # ~55개
db/.venv/bin/pytest tests -x -q 2>/dev/null || db/tests/run.sh  # 환경에 따라

# admin-ui (node)
cd admin-ui && npx vitest run --reporter=dot 2>&1 | tail -5
```

### 5. 커밋 — 어느 head를 머지했는지 반드시 기록

```bash
git commit -m "merge: origin/pr/nds-delta ... (<새 head sha 7자리>)"
```

다음 머지의 "이전 head" 비교 기준이 되므로 sha를 메시지에 넣습니다.

## 알려진 함정 카탈로그

머지 때마다 확인할 항목 — 새로 발견되면 여기에 추가합니다:

- **`session.info` 테스트 double** — 실제 `AsyncSession.info`는 dict인데
  `AsyncMock` 세션은 mock을 반환해 `.pop()`이 코루틴이 된다. 프로덕션 코드에
  `isinstance` 가드를 두지 말고 **테스트 double에 `.info = {}`를 명시**한다
  (conftest의 기존 패턴).
- **`--theirs` 파일 전체 교체** — 재제출 베이스(upstream 쪽)에 없는 NDS 커밋이
  섞인 파일에 쓰면 NDS 수정이 통째로 삭제된다. `messages.py`(1h 캐시 TTL
  과금, `anthropic_error` 스키마)가 대표 사례.
- **삭제-부활 (delete-vs-modify)** — 우리가 삭제한 파일이 PR 쪽에서 수정되면
  머지가 그 파일을 **부활**시킨다. upstream 기준으론 컴파일되지만 우리 트리가
  의도적으로 지운 심볼을 참조해 `tsc --noEmit`/`next build`가 깨진다
  (실제 발생: `RateLimitConfigPanel.tsx`→`RateLimitTreeNode`,
  `lib/actions/index.ts`→`create*Action`, `30261671`/`0cd0f626` 삭제분 부활).
  머지 후 검사: `git diff <prev>..HEAD --diff-filter=A` + `git log --diff-filter=D
  --follow` 교차 — 머지가 추가한 파일 중 우리 삭제 이력이 있으면 부활 의심.
- **값 스키마 분기** — per-1M vs per-1K 단가 UI 컨벤션처럼 "같은 필드의 다른
  표현"은 충돌로 표시되지 않고 조용히 둘 다 살아남는다. diff를 읽어 의미를
  맞춰야 한다.

## 운영 원칙

1. **PR이 upstream에 머지되면 소스를 전환** — 그 시점부터 `nds/dev`는
   `pr/nds-delta`가 아니라 upstream main을 따라간다. PR 브랜치는 미머지
   변경의 운반체일 뿐이다.
2. **긴급 픽스는 cherry-pick 예외** — PR 리뷰가 막혔는데 nds/dev에 당장
   필요한 upstream 변경이 있으면 해당 커밋만 `git cherry-pick`. 나중에
   정식 PR 머지분이 들어올 때 같은 내용이라 충돌 없이 지나간다.
3. **의미적 분기는 최소화** — 같은 로직의 다른 표현(단가 표기 컨벤션 등)은
   머지할 때마다 영구 비용이다. NDS 컨벤션을 PR로 올려 수렴시키거나
   upstream을 채택한다. 현재 분기 목록:
   - 단가 UI: NDS = per-1M 표기(`fmtPricePerM`, `step=0.00001`),
     upstream = per-1K 8자리(`formatRate`, `step=0.00000001`)
4. **새 함정은 이 문서에 추가** — 머지 중 잡은 자동머지 결함·테스트 double
   함정을 위 카탈로그에 누적한다.