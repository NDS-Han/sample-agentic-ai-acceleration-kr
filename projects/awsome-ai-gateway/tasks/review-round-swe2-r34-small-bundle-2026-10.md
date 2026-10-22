# R34 — 소형 묶음 리뷰 (gateway-clients + litellm-pricing-lambda + mock + scripts)

날짜: 2026-10 · 대상: ~1k LOC — 격리 컨테이너 클라이언트, pricing lambda, 로컬 mock, 온보딩/IAM 스크립트
베이스라인: litellm-pricing-lambda pytest 6/6 passed (코드 변경 없음 — 발견은 판정 보류만).

## 범위

`gateway-clients/` — `gw.sh`(빌드·VK발급·실행 헬퍼), `claude-box/`(Dockerfile+settings+statusline), `codex-box/`(Dockerfile+entrypoint: plane/tier → config.toml 생성), 문서 2종.
`litellm-pricing-lambda/` — 카탈로그 프록시 Lambda + 테스트.
`mock/main.py` — 로컬 dev용 OpenAI 호환 mock.
`scripts/` — `onboard-macos-linux.sh`, `create_cowork_mantle_iam.sh`.

## 발견·수정

코드 결함 없음 — 판정 보류 3건만 기록.

| # | 심각도 | 파일 | 관찰 | 판정 |
|---|---|---|---|---|
| 1 | N | `scripts/create_cowork_mantle_iam.sh` | `.env`를 **라인 번호로** 크리덴셜 추출 (`sed -n '4p'` …) — .env에 한 줄만 끼어들어도 다른 키를 집음 | 보류 — Part A/B 모두 `get-caller-identity`로 계정번호를 검증하고 불일치 시 ABORT → 깨지면 **실행 전 중단**(fail-closed)이지 오조작 아님. 일회성 부트스트랩이라 키명 파싱 개선은 다음 수정 기회에 |
| 2 | N | `gw.sh` | VK를 `docker run -e`로 컨테이너 주입 → `docker inspect`에 노출 | 보류 — 로컬 단일 사용자 dev 도구 + 1시간 만료 VK + `--rm` 임시 컨테이너. 대안(env 파일·stdin) 대비 복잡도 이득 없음 |
| 3 | N | `gw.sh` `claude`/`shell` | `read_vk`가 만료 VK를 그대로 전달 — 1시간 후 401에서야 인지 | 보류 — 헤더 주석이 만료→`gw.sh vk` 재실행을 명시, helper 없는 경량 도구의 의도된 단순함 |

## 검증된 경화 (변경 없음)

- **`gw.sh`**: VK 파일 `(umask 077; printf >)` — 생성 시점에 0600(TOCTOU 창 제거, 주석에 근거 명시); `CODEX_ENV_ARGS`를 `${arr[@]+"${arr[@]}"}` 관용구로 펼쳐 bash<4.4의 빈 배열 unbound 크래시 회피; 설정된 knob만 `-e` 전달해 호스트 기본값 무관하게 컨테이너 기본값 보존; `print_budget`은 stderr 전용(stdout 계약 비오염); usage 출력이 고정 행범위가 아닌 awk 헤더 스캔(주석이 실제 회귀를 방지한 이력 있음); engine 자동 docker→finch 폴백.
- **`codex-box/entrypoint.sh`**: `GW_TIER`/`GW_PLANE`/`GW_CONTEXT_WINDOW` **즉시 검증** — 오타가 미등록 alias로 서버 조용 폴백(200에 과금 오인)되는 것을 클라이언트에서 차단; TOML 인젝션 가드(`"`/개행 거부); 기본 plane=mantle로 기존 사용자 과금 plane 보존, `GW_MODEL` override 시 "우리가 안다고 주장하지 않음" 주석 포함 명시 경고; plane 분기표(0025/0032 마이그레이션 맥락)가 상단에 완비.
- **`claude-box`**: uid 1000 비root, VK 이미지 미베이크, `statusline.sh` stdout 단일행 + 조회 실패 시 graceful 출력(Claude Code statusLine 계약 준수).
- **`litellm-pricing-lambda`**: cost-map 모드(`LITELLM_COST_MAP_URL`) — `litellm_provider` 필드에 쉼표 필터, provider 없는 row는 필터에 fail-closed; 200+`errors` fail-soft(FunctionError로 admin-api를 죽이지 않음); `MAX_PAGES=50` 상한; `_fetch_page`의 `has_more` 루프; PAGE_SIZE env 검증 폴백; 반환 row 비가공(스키마 변경 내성). 테스트 6건 전부 통과.
- **`mock/main.py`**: usage 필드 정확, SSE `data:`/usage chunk/`[DONE]` 순서 정상 — dev 전용, 게이트·인증 없음이 용도상 적절.
- **`onboard-macos-linux.sh`**: 필수 env 4개 선검증, `curl --connect-timeout/--max-time`(블랙홀 hang 방지), `--setup-claude-code` opt-in 게이트, setup에 issuer/client 명시(누락 시 api-key-helper의 무음 STS 폴백 방지 — 주석에 근거), health 실패해도 비치명 경고.

## 잔여 과제

- `.env` 라인 기반 파싱 → 키명 파싱 전환 (안전하나 UX 불편 — ABORT 메시지가 원인을 안 가리킴).
- R32 문서의 installer↔cowork 드리프트 체크 자동화는 그대로 유효 — 본 라운드에서 추가 변종 사례 없음.
