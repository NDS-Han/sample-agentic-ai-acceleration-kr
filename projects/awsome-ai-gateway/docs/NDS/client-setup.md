# 직원 PC 클라이언트 연결 — Claude Code · Cowork · Codex (CLI + Desktop)

> **누가**: 직원(또는 세팅해 주는 IT). **목표**: 5분 안에 클라이언트를 게이트웨이에 연결.
> 인증 구조는 넷 다 같다 — **`gateway-cli login` 한 번**이면 모두 같은 키 체계
> (`api-key-helper` → Virtual Key)를 공유한다.
>
> OS는 **macOS · Linux · Windows · Windows WSL** 넷을 커버한다 — 각 절의 OS 표를
> 보고 내 쪽 블록만 실행하면 된다. 상세·실측 노트는 `../us-llm-gateway/` 의
> 클라이언트별 문서 참조.
>
> **운영자(배포·빌드·프로비저닝 담당)는 [client-admin.md](client-admin.md)** —
> 이 문서는 앤드유저용이다.

## 내 조합 바로가기

모든 조합이 **[§1 공통](#1-공통--딱-한-번-어떤-클라이언트든)(로그인→VK) → 아래 해당 절** 순서다.

| | Claude Code (CLI) | Cowork (Claude Desktop) | Codex (CLI+Desktop) |
|---|---|---|---|
| **macOS** | [§2-A](#a-claude-code-cli) | [§2-B-mac](#b-mac--helper-스크립트--프로필-macos) | [§2-C](#c-codex-cli--desktop--설정-공유) |
| **Linux** | [§2-A](#a-claude-code-cli) | — (데스크톱 앱 없음) | [§2-C](#c-codex-cli--desktop--설정-공유) |
| **Windows** | [§2-A](#a-claude-code-cli) | [§4 인스톨러](#4-windows--cowork--codex-desktop-상세) | [§2-C](#c-codex-cli--desktop--설정-공유) + [§4](#codex--windows) |
| **WSL** | [§2-A](#a-claude-code-cli) (⚠️ Windows 경로) | Windows 절차 적용 | [§2-C](#c-codex-cli--desktop--설정-공유) (⚠️ 바이너리 위치 확인) |

---

## 0. 시작 전 — 운영자에게 받을 것 (3가지)

| 필요한 것 | 운영자가 찾는 곳 | 없으면 |
|---|---|---|
| **env 4줄** | 배포 머신에서 `./deploy client-values` 출력 | 로그인 명령을 못 침 |
| **로그인 계정** | OIDC 프로바이더(Cognito 등)에 생성된 사용자 — 이메일+비번 | 브라우저 로그인 실패 |
| **내 PC IP 허용** | `network.allowed_cidrs`(gateway.yaml) + backend SG/방화벽 | 로그인은 되는데 **키발급·추론만 타임아웃** |

**게이트웨이 URL 은 `domain.mode` 로 결정됩니다** (`gateway-yaml.md` 참조):

| `domain.mode` | `ANTHROPIC_BASE_URL` | `ADMIN_API_URL` |
|---|---|---|
| `route53-acm` (`name: example.com`) | `https://gateway.example.com` | `https://admin-api.example.com` |
| `cloudfront-temp` (EKS) | `https://<배포된 CloudFront 도메인>` | `https://admin-<도메인>` |
| `none` (compose/평가) | `http://<호스트>:8000` | `http://<호스트>:8080` |

> 🔴 **Cowork(Claude Desktop)는 `https://` 필수** — `domain.mode: none` 이면
> Cowork 는 동작하지 않습니다(Claude Code·Codex는 됨).

```bash
# 운영자가 준 4줄 — 예시. 그대로 복붙 금지
export OIDC_ISSUER_URL="https://cognito-idp.<region>.amazonaws.com/<pool-id>"
export OIDC_CLIENT_ID="<app-client-id>"
export ADMIN_API_URL="https://<admin-api 주소>"
export ANTHROPIC_BASE_URL="https://<게이트웨이 주소>"
```

Windows PowerShell 은 `$env:` 로 같은 이름으로 설정합니다:

```powershell
$env:OIDC_ISSUER_URL="https://cognito-idp.<region>.amazonaws.com/<pool-id>"
$env:OIDC_CLIENT_ID="<app-client-id>"
$env:ADMIN_API_URL="https://<admin-api 주소>"
$env:ANTHROPIC_BASE_URL="https://<게이트웨이 주소>"
```

---

## 1. 공통 — 딱 한 번 (어떤 클라이언트든)

| OS | ① 설치 + 로그인 |
|---|---|
| **macOS / Linux / WSL** | 아래 bash 블록 (셋 다 동일 — WSL 도 리눅스 블록을 그대로 실행) |
| **Windows** | 아래 PowerShell 블록 — **관리자 PowerShell** |

### macOS / Linux / WSL (bash)

```bash
# ① uv + gateway-cli 설치 — ⚠️ 반드시 이 저장소에서 설치. upstream 은
#    macOS/WSL managed-settings 경로 버그로 조용히 게이트웨이를 우회한다.
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
cd <클론한 저장소>/projects/awsome-ai-gateway
uv tool install --from ./gateway-cli gateway-cli

# ② 로그인 — 브라우저가 자동으로 열린다 (OIDC 계정 입력)
#    WSL: 브라우저는 Windows 쪽이 열린다 — 콜백이 localhost:8090 으로 돌아오면 OK.
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"

# ③ 확인 — vk- 한 줄이 나오면 인증 끝
api-key-helper 2>/dev/null | grep -m1 '^vk-'
```

### Windows (관리자 PowerShell)

```powershell
# ① Python 3.11+ + git 확인 후 이 저장소에서 설치 (uv 도 가능)
pip install <클론한 저장소>\projects\awsome-ai-gateway\gateway-cli
#    gateway-cli / api-key-helper 가 Scripts\ 에 깔림 — PATH 에 없으면 추가

# ② 로그인 — 브라우저가 자동으로 열린다
gateway-cli login --issuer-url "$env:OIDC_ISSUER_URL" --client-id "$env:OIDC_CLIENT_ID"

# ③ 확인 — vk- 한 줄이 나오면 인증 끝
api-key-helper | Select-String '^vk-'
```

> 이미 다른 클라이언트를 게이트웨이로 쓰는 PC 라면 **①~③ 생략** — 토큰을 공유한다.
> WSL 과 Windows 는 토큰 저장소가 다르므로(WSL= `~/.gateway-cli`, Windows= `%USERPROFILE%`)
> 양쪽에서 쓰려면 각각 ①~③을 실행한다.

---

## 2. 클라이언트별 — 한 블록씩

### A. Claude Code (CLI)

| OS | 명령 | setup 이 쓰는 위치 |
|---|---|---|
| macOS | `sudo gateway-cli setup ...` | `/Library/Application Support/ClaudeCode/managed-settings.d/` |
| Linux | `sudo gateway-cli setup ...` | `/etc/claude-code/managed-settings.d/` |
| Windows | `gateway-cli setup ...` (관리자 PS) | `C:\Program Files\ClaudeCode\managed-settings.d\` |
| **WSL** | `sudo gateway-cli setup ...` | **`/mnt/c/Program Files/ClaudeCode/managed-settings.d/`** — WSL 의 Claude Code 는 **Windows 바이너리**라 Windows 경로를 읽는다(fork 가 자동 분기) |

```bash
# macOS / Linux / WSL — 공통 한 줄 (managed-settings 가 최상위 우선순위)
sudo gateway-cli setup \
  --gateway-url "$ANTHROPIC_BASE_URL" \
  --admin-api-url "$ADMIN_API_URL" \
  --issuer-url "$OIDC_ISSUER_URL" --client-id "$OIDC_CLIENT_ID"
```

```powershell
# Windows — 관리자 PowerShell
gateway-cli setup `
  --gateway-url "$env:ANTHROPIC_BASE_URL" `
  --admin-api-url "$env:ADMIN_API_URL" `
  --issuer-url "$env:OIDC_ISSUER_URL" --client-id "$env:OIDC_CLIENT_ID"
```

검증: `claude` → `/status` 에 `Auth token: apiKeyHelper` + `Enterprise managed settings`.

> 🔴 macOS/WSL: 위 표의 경로에 파일이 들어가야 읽힌다 — 그래서 **이 저장소의
> gateway-cli 필수**(upstream 은 두 곳 다 틀린 경로에 써서 조용히 우회됨).
> WSL 에서 `/status` 에 managed settings 가 안 보이면 Windows 쪽 `C:\Program
> Files\ClaudeCode\managed-settings.d\` 를 확인.

### B. Claude Code Desktop (Cowork)

| OS | 방법 |
|---|---|
| macOS | helper 스크립트 + 관리형 `.mobileconfig` (아래 블록) |
| Windows | **인스톨러 .exe**(권장) 또는 수동 HKLM 정책 — [§4](#4-windows--cowork--codex-desktop-상세) 참조 |
| Linux | **해당 없음** — Claude Desktop 은 macOS/Windows 전용 |
| WSL | **Windows 절차로 설치** — 데스크톱 앱은 Windows 쪽에서 돈다(WSL 안이 아님) |

`inferenceCredentialHelper` 는 **관리형 키**라 앱 UI 에 넣으면 조용히 무시된다.

#### B-mac — helper 스크립트 + 프로필 (macOS)

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

# ② 프로필 생성 → 설치
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

### C. Codex (CLI + Desktop — **설정 공유**)

| OS | config.toml 위치 | helper |
|---|---|---|
| macOS / Linux | `~/.codex/config.toml` | `/usr/local/bin/llm-gateway-helper.sh` (B-①과 동일) |
| Windows | `%USERPROFILE%\.codex\config.toml` | `api-key-helper` 를 부르는 `.cmd` 래퍼 (아래) |
| WSL | `~/.codex/config.toml` (WSL 홈) | mac/linux 블록과 동일 — 단 Codex 가 Windows 바이너리면 **Windows 표**를 따른다 |

```bash
# macOS / Linux / WSL — ① helper 는 B-① 과 동일(공용), ② config.toml
cat > ~/.codex/config.toml <<EOF
model = "gpt-6.1-sol"
model_provider = "gateway"

[model_providers.gateway]
name = "LLM Gateway"
base_url = "$ANTHROPIC_BASE_URL/v1"
wire_api = "responses"
requires_openai_auth = false

[model_providers.gateway.http_headers]
# Codex Desktop 앱은 codex 계열 헤더를 안 보내 게이트웨이에서 client='other' 로
# 분류돼 403 — codex prefix originator 를 명시해 라우팅/과금이 codex 로 잡히게 한다.
originator = "codex_desktop"

[model_providers.gateway.auth]
command = "/usr/local/bin/llm-gateway-helper.sh"
timeout_ms = 10000
refresh_interval_ms = 300000
EOF
```

```powershell
# Windows — ① helper 는 두 경로 중 하나:

#  [A] gateway-cli-cowork 인스톨러가 이미 깔려 있으면 (권장 — env 가 빌드에 베이크돼
#      래퍼 없이 exe 단독으로 vk- 한 줄을 뱉는다). 검증 후 그대로 auth.command 에 지정:
& 'C:\Gateway-CLI-Cowork\api-key-helper.exe'   # vk- 한 줄 확인

#  [B] gateway-cli(pip/uv 설치본)만 있으면 — env 를 박은 .cmd 래퍼 필요.
#      Desktop 앱은 쉘 환경변수를 상속 못 하므로 helper 안에서 env 를 세팅해야 한다.
$dir = "$env:USERPROFILE\bin"; New-Item -ItemType Directory -Force $dir | Out-Null
@"
@echo off
set OIDC_ISSUER_URL=$env:OIDC_ISSUER_URL
set OIDC_CLIENT_ID=$env:OIDC_CLIENT_ID
set ADMIN_API_URL=$env:ADMIN_API_URL
api-key-helper 2>nul | findstr /b "vk-"
"@ | Set-Content "$dir\llm-gateway-helper.cmd" -Encoding ascii
& "$dir\llm-gateway-helper.cmd"   # vk- 한 줄 확인

# ② config.toml — 내용은 macOS 와 동일, auth.command 만:
#   [A] command = 'C:\Gateway-CLI-Cowork\api-key-helper.exe'
#   [B] command = 'C:\Users\<나>\bin\llm-gateway-helper.cmd'
```

`config.toml` 키 설명:

- `wire_api = "responses"` — 필수. 게이트웨이의 codex 경로는 `/v1/responses` 만 받는다.
- `model = "gpt-6.1-sol"` — 게이트웨이 카탈로그의 ACTIVE alias 를 직접 지정
  (실측: `gpt-6.1-sol`). 등록 안 된 문자열은 서버가 routing_profile `default_model`로
  조용히 폴백한다 — 의도한 모델인지 대시보드 `usage_logs`로 확인할 것.
- `originator = "codex_desktop"` — Desktop 앱 필수(CLI 는 자동 전송). 없으면
  `client='other'` 로 분류돼 라우팅 프로필에 따라 403.
- ⚠️ **전제**: 운영자가 codex 라우팅을 켜둬야 한다 — admin-ui 의 routing profile 에
  `client='codex'` enabled + 해당 alias ACTIVE + 키 scope 포함.
- **Codex Desktop (앱) 전용**: 26.1013+ 에서 "work browser access requires an
  authenticated account" 에러가 나면 `[desktop]` 섹션에 추가 — work 모드(브라우저/
  컴퓨터 제어)가 ChatGPT 계정 로그인을 요구하는데 커스텀 provider 로는 그 게이트를
  못 통과한다:
  ```toml
  [desktop]
  conversationDetailMode = "STEPS_COMMANDS"
  ```
  (최상단이 아니라 `[desktop]` 키임에 주의. work 모드 기능 자체가 필요하면 앱에
  ChatGPT 계정 로그인이 필요 — 게이트웨이 추론은 로그인 없이 동작)

③ `codex` 실행 → 짧은 작업 → 대시보드 `client=codex` 확인.

> ✅ **실기기 검증 완료** — macOS(helper.sh + `gpt-6.1-sol`, originator 포함)와
> Windows(`api-key-helper.exe` 직접 지정 + `.cmd` 래퍼 양쪽) 모두 Desktop 앱에서
> 동작 확인됨(2026-10).

---

## 3. 안 될 때 — 증상으로 바로 진단

| 증상 | 원인 · 조치 |
|---|---|
| 로그인은 되는데 helper/추론이 타임아웃 | 내 IP 가 `allowed_cidrs`/SG 밖 → 운영자에게 등록 요청 |
| `apiKeyHelper failed` (연결 에러) | 같은 원인 — 인증이 아니라 **네트워크** |
| `403 Model not allowed` | 요청 모델이 키 scope 밖 → 운영자에게 `allowed_models` 확인 |
| Claude Code `/status` 에 apiKeyHelper 없음 | upstream gateway-cli 버그본 → 이 저장소에서 재설치 + `setup` |
| WSL 인데 managed settings 가 안 잡힘 | 파일이 `/mnt/c/...` 아닌 `/etc/`에 감 → fork gateway-cli 로 `setup` 재실행 |
| Cowork 가 helper 를 안 부름 | 프로필이 Local 레이어 → 관리형(.mobileconfig/HKLM)으로 재설치 |
| Cowork 연결 자체가 안 됨 | `ANTHROPIC_BASE_URL` 이 `http://` → https 도메인 필요 |
| Codex `401` | helper 경로 오타·실행권한 없음 → `chmod +x` + 직접 실행 확인 |
| Codex `403` (`client='other'`) | `originator` 헤더 누락 → `[model_providers.gateway.http_headers]` 에 `codex_desktop` 추가 (주석과 같은 줄에 붙어 삼켜지는 실수 주의) |
| Codex Desktop "work browser access requires an authenticated account" | 26.1013+ work 모드가 ChatGPT 로그인 요구 → `[desktop]` 에 `conversationDetailMode = "STEPS_COMMANDS"` 후 앱 완전 종료·재시작 |
| Codex `provider auth command ... exited 1` / `지정된 경로를 찾을 수 없습니다` | `auth.command` 경로의 파일이 실제 없음 → `Test-Path` 로 확인 후 존재하는 경로로 교체 |

**최종 확인 공통**: 아무 클라이언트에서 짧은 요청 한 번 → admin-ui 사용량에
`client=` 값과 함께 잡히면 끝.

---

## 4. Windows — Cowork / Codex Desktop 상세

### Cowork — Windows 두 경로

| 경로 | 언제 | 무엇을 |
|---|---|---|
| **인스톨러 빌드** (권장, 직원 다수) | 조직 배포 | BUILD PC 에서 `installer/packaging/site-config.json` 에 env 값을 박고 `build.ps1` → **단일 .exe** 를 직원에게 배포. 직원 PC 에서는 `gateway-cli-cowork setup`(HKLM 정책 기록) → 사용자별 `login` 한 번 |
| **수동** | 소수·검증 | `gateway-cli` 설치·로그인 → helper 스크립트 → Cowork `.msix` 설치 → 관리자 PowerShell 로 HKLM `inference*` 정책 6개 기록 |

상세: `../us-llm-gateway/cowork/installer/cowork-installer-admin-e2e-windows.md`(인스톨러) ·
`../us-llm-gateway/cowork/manual/cowork-client-install-windows.md`(수동, Windows Server 2025 실측 완료).

### Codex — Windows

`config.toml` 경로만 다르고 **내용은 macOS 와 동일**합니다:

- 설정 파일: `%USERPROFILE%\.codex\config.toml`
- helper `auth.command` — 두 경로 중 하나 (§2-C Windows 블록과 동일):
  - `gateway-cli-cowork` 인스톨러가 있으면 `C:\Gateway-CLI-Cowork\api-key-helper.exe`
    **직접 지정** (OIDC env 가 빌드에 베이크돼 래퍼 불요 — 실측 동작)
  - 아니면 env 를 박은 `.cmd` 래퍼를 만들어 그 절대경로 지정 — **존재하지 않는
    `C:\ProgramData\llm-gateway\helper.cmd` 같은 경로를 쓰면 `exited 1` 로 실패**
- Codex Desktop 도 같은 파일을 읽으므로 한 번 설정으로 둘 다 됩니다
- Desktop 앱은 `originator` 헤더가 필수(§2-C 키 설명 참조) — 없으면 `client='other'` 403

---

## 운영자 메모 — 직원에게 넘길 값은 어디서 나오나

> 관리자용 전체 절차(빌드·배포·프로비저닝·서버측 진단)는 **[client-admin.md](client-admin.md)**.

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
