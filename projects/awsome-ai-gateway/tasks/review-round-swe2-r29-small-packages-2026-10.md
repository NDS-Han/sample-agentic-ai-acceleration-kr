# R29 — 소규모 패키지 리뷰 (gateway-clients / litellm-pricing-lambda / mock / scripts)

**라운드 방식**: SWE-2 직접 리뷰. 전 파일 정독(~700줄) + 셸 가드 실측 + lambda 테스트.
**범위**: `gateway-clients/`(gw.sh + claude/codex-box), `litellm-pricing-lambda/`, `mock/`, `scripts/`.

## 처리한 발견

- **`gw.sh` VK 파일 TOCTOU** — `printf > ~/.gateway-vk; chmod 600` → 생성~chmod 사이 창에 umask 기본(대개 644)으로 VK 노출 → `(umask 077; printf >)` 생성 시점부터 600.
- **`codex-box/entrypoint.sh` TOML 깨짐 두 갈래** — `GW_CONTEXT_WINDOW=abc` → `model_context_window = abc` invalid TOML로 codex 부팅 실패 → 숫자 가드. `GW_URL`/`GW_MODEL`의 `"`/개행 → TOML 문자열 파괴 → 문자 가드(실측으로 두 경로 모두 `exit 2` 확인).
- **`onboard-macos-linux.sh` 헬스체크 무타임아웃** — 블랙홀 엔드포인트에서 스크립트 hang → `--connect-timeout 5 --max-time 10`.
- **`litellm-pricing-lambda` `PAGE_SIZE` 비정수 env → cold-start 크래시** — try/except + 1..1000 범위 가드, 기본 100 폴백.

## 확인됨(수정 불요)

- `gw.sh` VK 발급 경로 — python3 `sys.exit` 실패 시 `set -e`로 중단(빈 VK 저장 안 됨), id_token 사용 근거 주석 정확, `CODEX_ENV_ARGS` 빈배열 전개·bash<4.4 호환 주석 정확.
- `entrypoint.sh` plane/tier 설계 — mantle 기본 유지(기존 사용자 과금 경로 불변), 서버 폴백/404 의미론 주석이 실제 동작과 일치.
- `statusline.sh` — `[ -z GW ] || [ -z VK ] &&` 우선순위 의도 정확, stdout 계약 유지.
- `litellm-pricing-lambda` — MAX_PAGES 상한, 요청 타임아웃, fail-soft 계약(200+errors) 일관.
- `mock/` — 개발용, 비root 이미지. `create_cowork_mantle_iam.sh` — .env 행번호 파싱은 취약하나 계정 불일치 시 get-caller-identity 가드가 abort(오용 방지).
- 두 Dockerfile — 비root, VK는 이미지에 굽지 않고 런타임 env.

## 잔여

- `create_cowork_mantle_iam.sh`의 .env 행번호 의존 — 가드가 오용을 막지만 .env 편집 시 무발동 실패 가능(의도된 one-time 스크립트라 유보).
