# R46 — NDS deploy 도구 심층 리뷰

날짜: 2026-10-26 · 대상: docs/NDS/deploy/* (cli·schema·capture·doctor·eks_apply·ecs_apply·render/*, ~250KB)
방식: 배포 도구 고위험 영역 중심 — 인젝션·파괴 명령·시크릿 처리.

## 검증 통과 (강한 패턴)

| 항목 | 결과 |
|---|---|
| 셸 인젝션 | subprocess 전부 리스트 인자, `shell=True` 0건 |
| 파괴 명령 게이트 | teardown 3개 백엔드(compose/ecs/eks) 전부 `_confirm_destroy` — **환경명 타이핑** 필수, `plan -destroy`를 게이트 전에 출력, 비대화형은 --yes 요구 |
| 시크릿 파일 | `os.open(O_CREAT, 0o600)` + `os.fchmod` — umask 경합 윈도우까지 차단 (render/common.py 주석이 이유를 명시) |
| 시크릿 프로세스 유출 | `oidc.client_secret`은 helm `--set`으로 미주입 — --set은 terraform output(엔드포인트·ARN)만. ps/로그 노출 없음 |
| apply 게이트 | `_confirm_apply` — plan 미리보기 후 확인, CI는 --yes |
| doctor | 읽기 전용 (plan -detailed-exitcode·describe·ps). compose exec는 진단 쿼리용 |
| capture | 시크릿 반환값 처리 → 쓰기는 cli.py가 0o600으로 수행 |

## 발견·수정

| # | 항목 | 조치 |
|---|---|---|
| F1 | cli.py `_post_apply_notes` — 시드 크로스계정 ARN 정리 안내가 "SQL 전용"으로 낡음. G2로 /apps UI 경로가 생겼는데 안내가 갱신 안 됨 | 수정 — "/apps → 크로스 계정 라우팅 카드, 또는 SQL"로 갱신 |

## 판정

배포 도구는 리뷰 기준에서도 높은 완성도 — 실수 유형별 스윕에서 유효한
발견은 안내 문구 드리프트 1건뿐. 남은 리스크는 구조적 범위:
terraform/helm/aws CLI에 위임된 부분은 각 도구의 게이트를 따름.
