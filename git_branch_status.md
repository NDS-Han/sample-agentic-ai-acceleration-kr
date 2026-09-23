
# Upstream 동기화 & 증분 PR 워크플로 (NDS-Han 포크 전용)

  

> 이 문서는 우리 포크의 브랜치 운영 절차를 설명한다. **upstream PR에 포함되면 안 되는

> 포크 내부 문서** — PR delta를 생성할 때 이 파일은 제외할 것.

  

## 1. 저장소 구조

  

```text

origin git@github.com:NDS-Han/sample-agentic-ai-acceleration-kr.git (우리 fork)

upstream https://github.com/gonsoomoon-ml/sample-agentic-ai-acceleration-kr.git (실제 개발)

```

  

- upstream은 **squash merge**를 사용한다 → 커밋 SHA가 우리 쪽과 달라도 내용은 같을 수 있고,

조상(merge-base)이 upstream 진행에 따라 전진하지 않는다.

- upstream의 실제 개발 브랜치는 `us/deploy-fixes`다.

- 리포지토리 루트가 아니라 `projects/awsome-ai-gateway/` 아래에 앱이 있다.

  

## 2. 브랜치 토폴로지

  

```text

upstream/us/deploy-fixes ← canonical 개발 (squash merge 방식)

│ 주기적 merge (또는 신규 커밋 cherry-pick)

▼

merge/74d1<-upstream ← upstream 수렴 전용 레인

│ merge (우리 코드 직접 커밋 금지)

▼

after-merge-fix ← 우리 개발 main (feature 작업 + upstream 반영 수신)

│

├─→ pr/ours-delta → origin push → upstream PR (스냅샷, 개발 금지, 재생성 방식)

└─→ india/deployment (production, 배포 확정 시점에 머지)

```

  

머지 방향은 항상 `merge/74d1<-upstream → after-merge-fix` **단방향**. 역방향 머지는

sync 레인을 오염시키므로 하지 않는다.

  

## 3. upstream 변경 흡수

  

```bash

git fetch upstream

git checkout 'merge/74d1<-upstream'

git merge upstream/us/deploy-fixes # 권장: 진짜 merge

# 충돌 해결 후

git checkout after-merge-fix

git merge 'merge/74d1<-upstream'

```

  

- **merge를 우선 권장.** 한 번 merge하면 merge-base가 생겨 이후 sync가 증분 3-way merge가 된다.

- cherry-pick을 계속 쓰려면 "마지막으로 반영한 upstream SHA"를 반드시 기록하고,

`git log <that-sha>..upstream/us/deploy-fixes`로 새 커밋만 순서대로 가져올 것.

(과거에 9개 커밋이 통째로 빠져 `web_search_loop.py` 헬퍼 미정의 등 실버그가 된 사례 있음.)

- cherry-pick이 아니라 **파일 단위 수렴**이 필요하면(중간에 끼어든 커밋들과 충돌할 때):

대상 파일이 upstream의 순수 부분집합인지 먼저 확인하고

`git checkout upstream/us/deploy-fixes -- <file>` 로 수렴시킨다.

  

## 4. 증분 PR 브랜치 재생성 (pr/ours-delta)

  

목적: upstream tip 기준 **우리 고유 변경만** 보이는 cross-fork PR을 만든다.

(`india/deployment`처럼 오래된 브랜치로 PR을 열면 upstream 자신의 코드까지

"우리 변경"으로 표시되어 +19,000줄짜리 diff가 된다.)

  

upstream은 브랜치 생성 권한이 없으므로 브랜치는 origin에 만들고, PR의 base를

`gonsoomoon-ml:us/deploy-fixes`로 잡는다 (cross-fork PR).

  

```bash

cd /home/ubuntu/github/sample-agentic-ai-acceleration-kr

git fetch upstream

  

# 1. 새 upstream tip에서 worktree 생성 (메인 트리가 더러워도 안전)

git worktree remove /tmp/pr-delta --force 2>/dev/null

git worktree add /tmp/pr-delta -b pr/ours-delta-v2 upstream/us/deploy-fixes

cd /tmp/pr-delta

  

# 2. 순수 우리 파일 계산 — 양쪽 다 건드린 파일(교집합)은 제외

BASE=$(git merge-base upstream/us/deploy-fixes after-merge-fix)

git diff --name-only "$BASE" upstream/us/deploy-fixes | sort > /tmp/upstream-touched.txt

git diff --name-only upstream/us/deploy-fixes after-merge-fix | sort > /tmp/our-delta.txt

comm -23 /tmp/our-delta.txt /tmp/upstream-touched.txt > /tmp/ours-only.txt

comm -12 /tmp/our-delta.txt /tmp/upstream-touched.txt > /tmp/overlap.txt # ← 수작업 대상

  

# 3. 순수 우리 파일 통째 적용

xargs -a /tmp/ours-only.txt git checkout after-merge-fix --

  

# 4. 교집합 파일: upstream 버전을 유지한 채 우리 추가분만 병합

# 각 파일에서 우리 고유 라인 확인:

# git diff upstream/us/deploy-fixes after-merge-fix -- <file> (우리 쪽 '+' 라인이 우리 추가분)

# upstream 쪽 '-' 라인(=우리가 지우게 될 upstream 내용)이 있으면 절대 통째 덮지 말 것.

  

# 5. 커밋 & force-push — 같은 브랜치명이면 열린 PR diff가 자동 갱신됨

git add -A

git commit -m "feat: NDS-Han 증분 (upstream tip @$(git rev-parse --short upstream/us/deploy-fixes))"

git push -f origin pr/ours-delta

```

  

### 검증 체크리스트 (커밋 전)

  

- `git diff upstream/us/deploy-fixes HEAD --numstat | awk '$2>0'` — 삭제가 있는 파일이

교집합 파일 외에 있으면 upstream 라인을 지우는 건 아닌지 확인.

- 아티팩트 제외: `tfplan`, `.terraform.lock.hcl` 변경분, org 값이 채워진 values

(계정 ID·도메인·Cognito pool 등). delta의 values 파일은 `CHANGE_ME` 템플릿 상태여야 함.

- 이 문서(`OURS-UPSTREAM-SYNC.md`)가 ours-only 목록에 잡히면 제외할 것.

  

### 2026-10 시점 교집합 파일 (재생성 시 재계산 필수 — upstream이 더 건드리면 늘어남)

  

```text

deployment/charts/llm-gateway/values-eks-fargate-dev.yaml → 우리 버전 채택

deployment/charts/llm-gateway/values-eks-fargate-prod.yaml → 우리 버전 채택

(우리의 global.devLoginEnabled/adminUiLogin 메커니즘이 PR 대상 기능이라

upstream의 DEV_LOGIN_ENABLED env 라인을 대체하는 것이 의도된 변경)

docs/us-llm-gateway/README.md → upstream 유지 + 우리 행(IN-01) 추가

docs/us-llm-gateway/update-scripts/README.md → upstream 유지 + 01a/08-irsa 행 추가

docs/us-llm-gateway/updates.md → upstream 유지 + IN-1 행 추가

docs/us-llm-gateway/updates.en.md → upstream 유지 + IN-1 행 추가

```

  

## 5. 알려진 주의점

  

- **스크립트 번호 충돌**: upstream의 `08-set-model-pricing.sh`와 우리의

`08-setup-notification-ses-irsa.sh`가 같은 08번대 — upstream 머지 후 리네이밍 필요할 수 있음.

- **PR도 squash로 머지되면** 조상 공유가 다시 안 생겨 다음 PR도 같은 절차가 필요.

근본 해결은 upstream 측 일반 merge 또는 우리 측 rebase 워크플로 전환.

- **stash 관리**: `stach before pull` 이름의 stash가 여러 개 쌓여 있었음.

브랜치 전환 전 `git stash list` 확인.

- 브랜치명 `merge/74d1<-upstream`의 `<` 문자가 인코딩 문제를 일으킨 적 있음 —

쉘에서 작은따옴표로 감쌀 것: `git checkout 'merge/74d1<-upstream'`.

  

## 6. 검증 환경 메모

  

- 이 환경에는 `pytest`가 없다 — Python 검증은 `python3 -m compileall` + 대상 로직 직접 실행으로.

- dev 배포: `install-eks.sh dev` (values-eks-fargate-dev.yaml이 org 값이 채워진

상태인지 먼저 확인 — 템플릿 상태로 돌리면 안 됨).

- 이미지: `rebuild-image.sh <service> dev` → values 태그 갱신 → install-eks.