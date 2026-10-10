# 클라이언트 연결 퀵스타트 — Claude Code · Cowork · Codex (CLI + Desktop)

> **누가**: 직원(또는 세팅해 주는 IT). **목표**: 5분 안에 4개 클라이언트 중 원하는 것을 게이트웨이에 연결.
> 인증 구조는 넷 다 같다 — **한 번만 `gateway-cli login`** 하면 모두 같은 키 체계(`api-key-helper` → VK)를 공유한다.
>
> 상세·OS별 함정·트러블슈팅: [client-install.md](client-install.md) (Claude Code) ·
> [cowork/cowork-client-install-macos.md](cowork/cowork-client-install-macos.md) ·
> [codex/codex-client-install-macos.md](codex/codex-client-install-macos.md)

---

## 0. 시작 전 — 운영자에게 받을 것 (3가지)

| 필요한 것 | 없으면 |
|---|---|
| **env 4줄** — `OIDC_ISSUER_URL` `OIDC_CLIENT_ID` `ADMIN_API_URL` `ANTHROPIC_BASE_URL` | 로그인 명령을 못 침 |
| **Cognito 계정** — 이메일 + 임시 비번 (첫 로그인 때 새 비번으로 변경 강제) | 브라우저 로그인 실패 |
| **내 PC 공인 IP 를 `inbound-cidrs` 에 등록** | 로그인은 되는데 **키발급·추론만 타임아웃** |

```bash
# 운영자가 준 4줄 — 아래는 예시, 그대로 복붙하면 안 됨
export OIDC_ISSUER_URL="https://cognito-idp.<region>.amazonaws.com/<pool-id>"
export OIDC_CLIENT_ID="<app-client-id>"
export ADMIN_API_URL="https://<admin-api 주소>"
export ANTHROPIC_BASE_URL="https://<게이트웨이 주소>"   # https:// 필수
```

---

## 1. 공통 — 딱 한 번 (어떤 클라이언트든)

```bash
# ① uv + gateway-cli 설치 — ⚠️ 반드시 우리 저장소(fork)에서. upstream 은
#    macOS managed-settings 경로 버그로 조용히 게이트웨이를 우회한다.
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
cd <클론한 저장소>/projects/awsome-ai-gateway
uv tool install --from ./gateway-cli gateway-cli

# ② 로그인 — 브라우저가 자동으로 열린다 (Cognito 계정 입력)
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"

# ③ 확인 — vk- 한 줄이 나오면 인증 끝
api-key-helper 2>/dev/null | grep -m1 '^vk-'
```

> 이미 Claude Code 를 게이트웨이로 쓰는 PC 라면 **①~③ 생략** — 토큰을 공유한다.

---

## 2. 클라이언트별 — 한 블록씩

### A. Claude Code (CLI) — 한 줄

```bash
sudo gateway-cli setup \
  --gateway-url "$ANTHROPIC_BASE_URL" \
  --admin-api-url "$ADMIN_API_URL" \
  --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
```

managed-settings(최상위 우선순위)에 주소 + `apiKeyHelper` 를 박는다 — **이걸로 끝**.
검증: `claude` → `/status` 에 `Auth token: apiKeyHelper`.

> 🔴 macOS 주의: `setup` 은 `/Library/Application Support/ClaudeCode/managed-settings.d/` 에
> 써야 읽힌다 — 그래서 **fork 의 gateway-cli 필수**(§1 참조). `/status` 에
> `Enterprise managed settings` 가 없으면 버그본이 깔린 것.

### B. Claude Code Desktop (Cowork) — helper + 프로필

`inferenceCredentialHelper` 는 **관리형 키(MDM-Only)** 라 앱 UI 에 넣으면 조용히 무시된다.
관리형 레이어에 프로필을 얹는다(MDM 서버 불필요):

```bash
# ① helper 스크립트 (Codex 와 공용 — 이미 있으면 생략)
sudo tee /usr/local/bin/llm-gateway-helper.sh >/dev/null <<EOF
#!/bin/bash
set -euo pipefail
export OIDC_ISSUER_URL="$OIDC_ISSUER_URL"
export OIDC_CLIENT_ID="$OIDC_CLIENT_ID"
export ADMIN_API_URL="$ADMIN_API_URL"
export HOME="\${HOME:-/Users/\$(id -un)}"
H="\$HOME/.local/bin/api-key-helper"
[ -x "\$H" ] || H="\$(command -v api-key-helper || true)"
[ -z "\$H" ] && { echo "api-key-helper not found" >&2; exit 1; }
"\$H" 2>/dev/null | grep -m1 '^vk-'
EOF
sudo chmod +x /usr/local/bin/llm-gateway-helper.sh
/usr/local/bin/llm-gateway-helper.sh   # vk- 한 줄 확인 (sudo 붙이지 말 것)

# ② 프로필 생성 → 더블클릭 설치 (env 가 박혀서 생성됨)
cat > ~/Downloads/us-llm-gateway-cowork.mobileconfig <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
 <key>PayloadContent</key><array><dict>
  <key>PayloadType</key><string>com.anthropic.claudefordesktop</string>
  <key>PayloadIdentifier</key><string>com.anthropic.claudefordesktop.us-llm-gateway</string>
  <key>PayloadDisplayName</key><string>US LLM Gateway - Cowork</string>
  <key>PayloadUUID</key><string>c5aedb63-b1d3-4d7c-940c-713c7cb47e4d</string>
  <key>PayloadVersion</key><integer>1</integer>
  <key>inferenceProvider</key><string>gateway</string>
  <key>inferenceGatewayBaseUrl</key><string>$ANTHROPIC_BASE_URL</string>
  <key>inferenceGatewayAuthScheme</key><string>bearer</string>
  <key>inferenceCredentialHelper</key><string>/usr/local/bin/llm-gateway-helper.sh</string>
  <key>inferenceCredentialHelperTtlSec</key><integer>1800</integer>
  <key>inferenceModels</key><array>
   <string>claude-sonnet-5-5</string><string>claude-opus-5-5</string>
   <string>claude-haiku-4-5-20251001</string></array>
 </dict></array>
 <key>PayloadDisplayName</key><string>US LLM Gateway Cowork</string>
 <key>PayloadIdentifier</key><string>com.anthropic.claudefordesktop.us-llm-gateway.profile</string>
 <key>PayloadType</key><string>Configuration</string>
 <key>PayloadUUID</key><string>bcee0872-d3bc-477c-a63e-3334114d5a04</string>
 <key>PayloadVersion</key><integer>1</integer>
</dict></plist>
EOF
open ~/Downloads/us-llm-gateway-cowork.mobileconfig   # 시스템 설정 → 설치 승인
```

③ 앱 완전 종료 → 재실행. 검증: 짧은 대화 → 대시보드 `client=cowork`.

### C. Codex (CLI + Desktop — **설정 공유**) — helper + config.toml

CLI 와 데스크톱이 **같은 `~/.codex/config.toml`** 을 읽는다 — 한 번만.

```bash
# ① helper 스크립트 — 위 B-① 과 완전히 동일 (이미 있으면 생략)

# ② config.toml — 기존 파일이 있으면 덮지 말고 블록 병합
cat > ~/.codex/config.toml <<EOF
model = "gpt-5.5"
model_provider = "gateway"

[model_providers.gateway]
name = "US LLM Gateway"
base_url = "$ANTHROPIC_BASE_URL/v1"
wire_api = "responses"
requires_openai_auth = false

[model_providers.gateway.auth]
command = "/usr/local/bin/llm-gateway-helper.sh"
timeout_ms = 10000
refresh_interval_ms = 300000
EOF
```

- `wire_api = "responses"` — 필수. 게이트웨이의 codex 경로는 `/v1/responses` 만 받는다.
- `model = "gpt-5.5"` — Codex 내장 메타데이터에 있는 실제 모델명(경고 없음). 게이트웨이는
  routing_profile 의 `default_model`(`codex-gpt`)로 라우팅한다. 게이트웨이 alias
  (`codex-gpt-5.6-terra` 등)를 직접 지정도 가능 — 단 ACTIVE + `allowed_models` 범위여야 함.
- ⚠️ **전제**: 운영자가 codex 라우팅을 켜둬야 한다(`client='codex'` 프로필 enabled +
  alias ACTIVE + 키 scope). 안 되면 `403 Model not allowed`.

③ `codex` 실행 → 짧은 작업 → 대시보드 `client=codex` 확인.

> ⚠️ 이 Codex 절차는 **실기기 검증 전**(구조는 Cowork 와 동일). 안 되면
> [codex/codex-client-install-macos.md](codex/codex-client-install-macos.md) §6 체크리스트.

---

## 3. 안 될 때 — 증상으로 바로 진단

| 증상 | 원인 · 조치 |
|---|---|
| 로그인은 되는데 helper/추론이 타임아웃 | 내 IP 가 `inbound-cidrs` 밖 → 운영자에게 등록 요청 |
| `apiKeyHelper failed` (연결 에러) | 같은 원인 — 인증이 아니라 **네트워크** |
| `403 Model not allowed` | 요청 모델이 키 scope 밖 → 운영자에게 `allowed_models` 확인 |
| Claude Code `/status` 에 apiKeyHelper 없음 | upstream gateway-cli 버그본 → fork 로 재설치 + `setup` 재실행 |
| Cowork 가 helper 를 안 부름 | 프로필이 Local 레이어 → 관리형(.mobileconfig)으로 재설치 |
| Codex `401` | helper 경로 오타·실행권한 없음 → `sudo chmod +x` + helper 직접 실행 확인 |

**최종 확인 공통**: 아무 클라이언트에서 짧은 요청 한 번 → 대시보드/admin-ui 의
사용량에 `client=` 값과 함께 잡히면 끝.
