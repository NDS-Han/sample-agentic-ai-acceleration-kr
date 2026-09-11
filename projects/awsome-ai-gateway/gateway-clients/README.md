# gateway-clients — 로컬 격리 컨테이너로 LLM Gateway 사용

호스트 맥 환경(`~/.claude`, `~/.codex`, 셸 설정)을 **전혀 건드리지 않고**, 격리 컨테이너에서
Claude Code / Codex 를 LLM Gateway 로 붙여 쓴다. 사용량·비용은 게이트웨이가 집계(앱별 client 구분).

> **Cowork 는 제외** — Cowork 는 macOS 데스크톱 GUI 앱이라 컨테이너(헤드리스)에서 실행 불가.
> 컨테이너로 되는 건 CLI 인 **claude-code** 와 **codex** 둘.

## 구성
```
gateway-clients/
  claude-box/Dockerfile   # node20 + @anthropic-ai/claude-code
  codex-box/Dockerfile    # node20 + @openai/codex (+ entrypoint 가 config.toml 생성)
  codex-box/entrypoint.sh # GW_URL/GATEWAY_VK/GW_PLANE/GW_TIER → ~/.codex/config.toml (wire_api=responses)
  gw.sh                   # 빌드·VK발급·실행 헬퍼
```

## 빠른 시작
```bash
cd gateway-clients

# 1) 이미지 빌드 (최초 1회)
./gw.sh build

# 2) VK 발급 (호스트 브라우저로 OIDC 로그인 → ~/.gateway-vk 저장, 1시간 유효)
./gw.sh vk
#   gateway-cli 가 호스트에 없으면, 발급받은 VK 문자열을 직접 저장도 가능:
#   printf '%s' '<VK>' > ~/.gateway-vk && chmod 600 ~/.gateway-vk

# 3) 사용 (현재 디렉터리가 /work 로 마운트됨)
./gw.sh claude "이 코드 리뷰해줘"
./gw.sh codex  "버그 고쳐줘"

# 디버그 셸
./gw.sh shell claude
./gw.sh shell codex
```

## 동작 원리
- **claude-box**: `ANTHROPIC_BASE_URL=<게이트웨이>` + `ANTHROPIC_AUTH_TOKEN=<VK>` 를 env 로 주입 →
  Claude Code 가 `/v1/messages` 를 게이트웨이로 호출. 게이트웨이가 `client=claude-code` 로 식별.
- **codex-box**: entrypoint 가 `~/.codex/config.toml` 을 생성(`base_url=<게이트웨이>/v1`,
  `wire_api="responses"`, `env_key=GATEWAY_VK`) → Codex 가 `/v1/responses` 로 GPT-5.6 호출.
  Codex 가 보내는 `originator` 헤더로 게이트웨이가 `client=codex` 식별 — 대화형은 `codex_cli_rs`,
  `codex exec` 는 `codex_exec` 이고 판정이 `startswith("codex")` 라서 둘 다 인식된다.

### codex 모델·plane 선택
GPT-5.6 은 Bedrock 의 **두 plane** 으로 서비스되고 plane 별로 alias 가 따로 등록돼 있다.
같은 모델이라도 alias 가 다르면 다른 plane·다른 과금 행이다.

| `GW_PLANE` | alias (terra 기준) | 인증 | invocation log |
|---|---|---|---|
| `mantle` (기본) | `codex-gpt-5.6-terra` | 단기 Bearer | ❌ 기록 안 됨 |
| `runtime` | `gpt-5.6-terra` | SigV4 + CRIS | ✅ 기록됨(본문 감사 가능) |

```bash
./gw.sh codex "버그 고쳐줘"                      # 기본 = mantle plane
GW_PLANE=runtime ./gw.sh codex "버그 고쳐줘"      # 표준 runtime plane
GW_PLANE=runtime GW_TIER=sol ./gw.sh codex ...   # tier: sol | terra(기본) | luna
```
`GW_MODEL=<alias>` 로 전체 override, `GW_CONTEXT_WINDOW=<int>` 로 context window override.
기본값을 `mantle` 로 둔 이유는 기존 사용자의 plane·과금·VK scope 를 바꾸지 않기 위해서다.
클라이언트가 보낸 model 이 ACTIVE alias 로 resolve 되면 그 값이 이기고, resolve 안 되면
routing profile 의 `default_model` 로 폴백한다 — 자세한 내용은 `codex-box/entrypoint.sh` 주석.
- **VK**: 호스트 `~/.gateway-vk`(plain 문자열, 600)에서 읽어 컨테이너에 env 주입. 호스트 환경 무변경.
  1시간 만료 → `./gw.sh vk` 재실행.

## 호스트 환경 보존 보장
- 이미지에 VK·자격증명을 굽지 않음(런타임 env 만).
- `~/.claude`, `~/.codex` 같은 호스트 설정 미접근(컨테이너 내부에만 생성).
- 컨테이너는 `--rm` 으로 매번 폐기, `$PWD` 만 `/work` 로 마운트.

## 주의
- 게이트웨이 dev ALB 는 HTTP(80) — 평문. dev 전용. (운영은 HTTPS 필요)
- codex 가 게이트웨이에서 거부(403 Model not allowed)되면, 해당 user/team 의 allowed_models 에
  **실제로 요청되는 alias** 를 추가해야 한다(admin UI 사용자/팀 또는 모델 권한). alias 는
  plane 마다 다르므로 `GW_PLANE` 을 바꾸면 필요한 권한도 바뀐다 — `mantle` →
  `codex-gpt-5.6-terra`, `runtime` → `gpt-5.6-terra`(구 환경은 `codex-gpt`).
  scope 검사는 model 폴백과 무관하게 항상 적용된다. claude-code 도 동일 원리.
- `GW_URL`/`ADMIN_URL` 은 `gw.sh` 상단에서 환경변수로 override 가능.
