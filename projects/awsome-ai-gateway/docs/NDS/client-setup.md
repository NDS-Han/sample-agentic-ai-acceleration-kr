# 직원 PC 클라이언트 연결 — Claude Code · Cowork · Codex (CLI + Desktop)

> **누가**: 직원(또는 세팅해 주는 IT). **목표**: 5분 안에 클라이언트를 게이트웨이에 연결.
> 인증 구조는 넷 다 같다 — **`gateway-cli login` 한 번**이면 모두 같은 키 체계
> (`api-key-helper` → Virtual Key)를 공유한다.
>
> 이 문서는 범용(NDS 배포) 기준입니다 — 사이트별 실측 상세·Windows 절차는
> `../us-llm-gateway/` 의 클라이언트별 문서를 참조하세요.

---

## 0. 시작 전 — 운영자에게 받을 것 (3가지)

| 필요한 것 | 운영자가 찾는 곳 | 없으면 |
|---|---|---|
| **env 4줄** | `deployment/gateway.yaml` 의 `oidc:`(`issuer_url`/`client_id`) + 배포된 URL(아래 표) | 로그인 명령을 못 침 |
| **로그인 계정** | OIDC 프로바이더(Cognito 등)에 생성된 사용자 — 이메일+비번 | 브라우저 로그인 실패 |
| **내 PC IP 허용** | `network.allowed_cidrs`(gateway.yaml) + backend SG/방화벽 | 로그인은 되는데 **키발급·추론만 타임아웃** |

**게이트웨이 URL 은 `domain.mode` 로 결정됩니다** (`gateway-yaml.md` 참조):

| `domain.mode` | `ANTHROPIC_BASE_URL` | `ADMIN_API_URL` |
|---|---|---|
| `route53-acm` (`name: example.com`) | `https://gateway.example.com` | `https://admin-api.example.com` |
| `cloudfront-temp` (EKS) | `https://<배포된 CloudFront 도메인>` | `https://admin-<도메인>` |
| `none` (compose/평가) | `http://<호스트>:8000` | `http://<호스트>:8080` |

> 🔴 **Cowork(Claude Desktop)는 `https://` 필수** — `domain.mode: none` 이면
> Cowork 는 동작하지 않습니다(Claude Code·Codex는 됨). Cowork를 쓰려면
> 도메인을 먼저 달아야 합니다.

```bash
# 운영자가 준 4줄 — 예시. 그대로 복붙 금지
export OIDC_ISSUER_URL="https://cognito-idp.<region>.amazonaws.com/<pool-id>"
export OIDC_CLIENT_ID="<app-client-id>"
export ADMIN_API_URL="https://<admin-api 주소>"
export ANTHROPIC_BASE_URL="https://<게이트웨이 주소>"
```

---

## 1. 공통 — 딱 한 번 (어떤 클라이언트든)

```bash
# ① uv + gateway-cli 설치 — ⚠️ 반드시 이 저장소에서 설치. upstream 은
#    macOS managed-settings 경로 버그로 조용히 게이트웨이를 우회한다.
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
cd <클론한 저장소>/projects/awsome-ai-gateway
uv tool install --from ./gateway-cli gateway-cli

# ② 로그인 — 브라우저가 자동으로 열린다 (OIDC 계정 입력)
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"

# ③ 확인 — vk- 한 줄이 나오면 인증 끝
api-key-helper 2>/dev/null | grep -m1 '^vk-'
```

> 이미 다른 클라이언트를 게이트웨이로 쓰는 PC 라면 **①~③ 생략** — 토큰을 공유한다.

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

> 🔴 macOS: `setup` 이 `/Library/Application Support/ClaudeCode/managed-settings.d/` 에
> 써야 읽힌다 — 그래서 **이 저장소의 gateway-cli 필수**. `/status` 에
> `Enterprise managed settings` 가 없으면 버그본이 깔린 것.

### B. Claude Code Desktop (Cowork) — helper + 프로필

`inferenceCredentialHelper` 는 **관리형 키**라 앱 UI 에 넣으면 조용히 무시된다.
관리형 레이어에 프로필을 얹는다(macOS 기준; Windows 는 us-llm-gateway 문서 참조):

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

# ② 프로필 생성 → 설치 (macOS)
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
name = "LLM Gateway"
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
  (예: `codex-gpt-5.6-*`)를 직접 지정도 가능 — ACTIVE + `allowed_models` 범위여야 함.
- ⚠️ **전제**: 운영자가 codex 라우팅을 켜둬야 한다 — admin-ui 의 routing profile 에
  `client='codex'` enabled + 해당 alias ACTIVE + 키 scope 포함. Mantle 크로스어카운트
  라우팅은 계정별 `account_role_arn` 설정이 별도로 필요할 수 있다(README §91 참조).

③ `codex` 실행 → 짧은 작업 → 대시보드 `client=codex` 확인.

> ⚠️ Codex 절차는 **실기기 검증 전** — 구조는 Cowork 와 동일한 `auth.command` 패턴이나
> 데스크톱의 config 검증이 더 엄격할 수 있다는 보고가 있다. 안 되면
> `../us-llm-gateway/codex/codex-client-install-macos.md` §6 체크리스트.

---

## 3. 안 될 때 — 증상으로 바로 진단

| 증상 | 원인 · 조치 |
|---|---|
| 로그인은 되는데 helper/추론이 타임아웃 | 내 IP 가 `allowed_cidrs`/SG 밖 → 운영자에게 등록 요청 |
| `apiKeyHelper failed` (연결 에러) | 같은 원인 — 인증이 아니라 **네트워크** |
| `403 Model not allowed` | 요청 모델이 키 scope 밖 → 운영자에게 `allowed_models` 확인 |
| Claude Code `/status` 에 apiKeyHelper 없음 | upstream gateway-cli 버그본 → 이 저장소에서 재설치 + `setup` |
| Cowork 가 helper 를 안 부름 | 프로필이 Local 레이어 → 관리형(.mobileconfig)으로 재설치 |
| Cowork 연결 자체가 안 됨 | `ANTHROPIC_BASE_URL` 이 `http://` → https 도메인 필요 |
| Codex `401` | helper 경로 오타·실행권한 없음 → `chmod +x` + 직접 실행 확인 |

**최종 확인 공통**: 아무 클라이언트에서 짧은 요청 한 번 → admin-ui 사용량에
`client=` 값과 함께 잡히면 끝.

---

## 운영자 메모 — 직원에게 넘길 값은 어디서 나오나

**한 번에 뽑기** (배포 머신에서):

```bash
./deploy client-values        # env 4줄을 출력 — 이 블록을 직원에게 전달
```

eks 는 `values-eks-*.local.yaml` 의 ingress host, ecs 는 terraform output
(`alb_dns_name`, `cognito_*`), compose 는 공인 IP 자동 탐지를 씁니다.
OIDC 가 비어 있거나 주소를 못 찾으면 `<TODO>` 로 표시하고 이유를 출력합니다.

| 값 | 출처 (client-values 가 읽는 곳) |
|---|---|
| `OIDC_ISSUER_URL` / `OIDC_CLIENT_ID` | `gateway.yaml` `oidc:` → 없으면 terraform output `cognito_*` |
| 게이트웨이/admin-api URL | eks: values `ingress.*.host` / ecs: `alb_dns_name` / compose+도메인: `gateway.<domain>` / compose 무도메인: 공인 IP:8000 |
| 사용자 계정 | OIDC 프로바이더(Cognito)에서 생성 — admin bootstrap 은 설치 문서 참조 |
| 클라이언트 IP 허용 | `network.allowed_cidrs` 수정 후 `./deploy apply` (SG 는 backend 가 갱신) |

---

## 4. Windows — Cowork / Codex Desktop 은 어떻게?

Windows 에서도 인증 구조는 동일(`gateway-cli login` → helper → VK)하지만
**관리형 설정의 저장 위치가 레지스트리(HKLM)**이고, 배포 방식이 두 갈래입니다:

### Cowork (Claude Desktop) — Windows

| 경로 | 언제 | 무엇을 |
|---|---|---|
| **인스톨러 빌드** (권장, 직원 다수) | 조직 배포 | BUILD PC 에서 `installer/packaging/site-config.json` 에 env 값을 박고 `build.ps1` → **단일 .exe** 를 직원에게 배포. 직원 PC 에서는 `gateway-cli-cowork setup`(HKLM 정책 기록) → 사용자별 `login` 한 번 |
| **수동** | 소수·검증 | `gateway-cli` 설치·로그인 → helper 스크립트 → Cowork `.msix` 설치 → 관리자 PowerShell 로 HKLM `inference*` 정책 6개 기록 |

상세: `../us-llm-gateway/cowork/installer/cowork-installer-admin-e2e-windows.md`(인스톨러) ·
`../us-llm-gateway/cowork/manual/cowork-client-install-windows.md`(수동, Windows Server 2025 실측 완료).

### Codex (CLI + Desktop) — Windows

`config.toml` 경로만 다르고 **내용은 macOS 와 동일**합니다:

- 설정 파일: `%USERPROFILE%\.codex\config.toml` (PowerShell 에서 `~\.codex\config.toml`)
- helper: mac 의 `/usr/local/bin/llm-gateway-helper.sh` 대신 `api-key-helper.exe` 를
  직접 가리키는 `.ps1`/`.cmd` 래퍼를 만들어 `auth.command` 에 절대경로로 지정
- Codex Desktop 도 같은 파일을 읽으므로 한 번 설정으로 둘 다 됩니다

> ⚠️ Windows Codex 데스크톱 경로는 **미실측** — `auth.command` 의 Windows 지원은
> Codex 버전에 따릅니다. CLI 에서 먼저 검증하고 데스크톱에 같은 설정을 적용하세요.
