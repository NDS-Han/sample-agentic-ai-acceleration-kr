# Codex(CLI · 데스크톱) 클라이언트 설치 — macOS

> **상태: CLI·데스크톱 실기기 검증 완료 — macOS.**
> `gateway-cli` 로그인 → `auth.command` helper → `wire_api="responses"` 경로로
> CLI·데스크톱 모두 종단 호출을 확인했습니다. ⚠️ **데스크톱은 `http_headers`
> 의 `originator` 명시가 필수**입니다(절차 4) — 앱이 codex 계열 originator 를
> 보내지 않아, 없으면 `client=other` 로 분류돼 403 이 납니다.

**Codex CLI** 와 **Codex 데스크톱(ChatGPT 앱 내 Codex / Codex Desktop)** 을
게이트웨이에 붙이는 문서입니다. 둘은 **같은 `~/.codex/config.toml`** 을 읽으므로
설정은 한 번만 합니다. 게이트웨이 쪽 변경(`update-scripts/` + codex 라우팅
활성화)이 끝난 뒤에 하십시오.

---

## 1. 한눈에

직원 Mac 의 Codex 를 회사 게이트웨이에 연결합니다. 열쇠(VK)는 Codex 가
`auth.command` 로 helper 를 실행해 자동 발급·갱신하므로 직원이 키를 관리할 일은
없습니다 — Cowork 의 `inferenceCredentialHelper` 와 같은 구조입니다.

| 절차 | 무엇을 하나 | 창 | 끝난 것을 아는 법 |
| --- | --- | --- | --- |
| **1** | `gateway-cli` 설치 + 회사 계정 로그인 | 🔵 | `api-key-helper` 가 `vk-` 한 줄 출력 |
| **2** | credential helper 작성 | 🔵 → 🔴 | helper 직접 실행 시 `vk-` 한 줄 |
| **3** | Codex 설치 (CLI / 데스크톱) | — | `codex --version` 출력 |
| **4** | `~/.codex/config.toml` 작성 | 🔵 | `codex` 가 게이트웨이로 호출 |
| **5** | 실행 → 짧은 작업 → 게이트웨이 기록 확인 | — | `usage_logs` 에 `client=codex` |

**창 표시**: ▶ 🔵 **Terminal**(직원 본인 계정) · ▶ 🔴 같은 창에서 **`sudo`** 를 붙인 명령 · ▶ 🟢 **운영자**가 배포 EC2 에서.
⚠️ `sudo` 없이 하라는 확인 명령에 `sudo` 를 붙이면 root 기준으로 돌아 토큰을 못 찾습니다.

**운영자에게 미리 받을 것** — ① env 값 4개(`07-client-values.sh` 출력) ② 로그인 계정(이메일+임시 비밀번호) ③ 이 Mac 공인 IP 의 `inbound-cidrs` 등록(`05-allow-client-ip.sh`).

> ⚠️ **게이트웨이 전제** — codex 라우팅이 활성화돼 있어야 합니다:
> `model.routing_profiles` 의 `client='codex'` 행 enabled, 프로필
> `default_model` 이 가리키는 alias ACTIVE, 그리고 해당 사용자/팀의
> `allowed_models` 에 그 alias 가 포함돼 있어야 합니다. 빠져 있으면
> `403 Model not allowed` 입니다.

---

## 2. 전제

**게이트웨이 쪽** — `update-scripts/README.md` 실행 순서 완료: `https://` base
URL(CloudFront `03-create-cloudfront.sh` 또는 ALB+Route53 등 배포 형태에
따름), codex 라우팅 프로필 + default alias ACTIVE, **클라이언트
공인 IP 등록(`05-allow-client-ip.sh`)**. IP 가 빠지면 로그인(공개)은 되는데
**VK 발급(IP 제한)만 타임아웃**납니다.

**클라이언트 쪽** — `gateway-cli` 와 로그인 토큰뿐. 같은 Mac 에서 **Claude Code
또는 Cowork 를 이미 쓰면 로그인을 공유하므로 절차 1 생략.** ⚠️ `gateway-cli` 는
반드시 **fork** 에서 설치(upstream 은 벤더 버그 픽스 3건 부재).

**망** — 아래 호스트가 막혀 있으면 안 됩니다.

| 호스트 | 언제 쓰나 | 막혀 있으면 |
| --- | --- | --- |
| 게이트웨이 주소 (`https://` URL) | 추론 요청 전부 | 응답 없음 |
| `OIDC_ISSUER_URL` | 로그인, VK 갱신 | 로그인/VK 불가 |
| `registry.npmjs.org` 등 | CLI 설치 시 | 설치 불가 |
| `chatgpt.com` 등 | 데스크톱 앱 다운로드·업데이트 | 앱 설치 불가 |

**설정 위치 (결론만)** — provider·auth 설정은 **반드시 사용자 레벨
`~/.codex/config.toml`** 에 넣습니다. 프로젝트 안의 `.codex/config.toml` 에
`model_provider`·`model_providers` 를 써도 Codex 가 **무시**합니다(시작 경고만
출력). 이는 Codex 공식 동작입니다.

---

## 3. 절차

### 절차 1. `gateway-cli` 설치 + 로그인

> 그 Mac 에서 **Claude Code / Cowork 를 쓰고 있다면 이 절 생략** — 토큰
> (`~/.gateway-cli/`)을 공유합니다.

**⓪ 사전 요구사항** — ▶ 🔵 `git --version`, `uv --version` 둘 다 찍히면 ① 로.
`git` 부재 시 Command Line Tools 설치 창 승인. `uv` 부재 시:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

끝나면 **새 터미널**에서 `uv --version` 확인.

**① 저장소** — ⚠️ 반드시 `gonsoomoon-ml` **fork**, 브랜치 `us/deploy-fixes`. ▶ 🔵

```bash
cd ~
git clone -b us/deploy-fixes \
  https://github.com/gonsoomoon-ml/sample-agentic-ai-acceleration-kr.git
cd ~/sample-agentic-ai-acceleration-kr/projects/awsome-ai-gateway
```

**② 설치** — ▶ 🔵

```bash
uv tool install --from ./gateway-cli gateway-cli
gateway-cli version
```

`command not found` 면 `uv tool update-shell` 후 **새 터미널**에서 재확인.

**③ 운영자에게 받은 값 4개** — 운영자가 배포 EC2 에서(🟢) `bash
07-client-values.sh` 를 돌려 준 **"macOS / Linux"** 절의 `export` 4줄을
붙여넣습니다. ▶ 🔵

```bash
export OIDC_ISSUER_URL="<from operator>"
export OIDC_CLIENT_ID="<from operator>"
export ADMIN_API_URL="<from operator>"
export ANTHROPIC_BASE_URL="<from operator - starts with https://>"
```

> `ANTHROPIC_BASE_URL` 은 이름과 달리 **게이트웨이 base URL** 입니다 — Codex 도
> 같은 주소를 씁니다(절차 4 의 `base_url`).

**④ 로그인** — ⚠️ **③ 을 넣은 그 창에서** (export 값이 그 창에서만 유효). ▶ 🔵

```bash
cd ~/sample-agentic-ai-acceleration-kr/projects/awsome-ai-gateway
bash scripts/onboard-macos-linux.sh
```

브라우저 로그인 화면에서 운영자 발급 계정으로 로그인 (첫 로그인 시 새 비밀번호
설정). 콜백 `localhost:8090` 이 점유돼 실패하면 — 등록 콜백은
`8090`·`8091`·`8092` **3개뿐** — `lsof -nP -iTCP:8090-8092 -sTCP:LISTEN` 으로
빈 포트를 골라:

```bash
gateway-cli login --issuer-url "$OIDC_ISSUER_URL" \
  --client-id "$OIDC_CLIENT_ID" --redirect-port 8091
```

**⑤ 확인** — ▶ 🔵

```bash
api-key-helper 2>/dev/null | grep -m1 '^vk-'
```

`vk-` 한 줄이면 완료. ⚠️ 로그인 성공 ≠ 완료 — VK 발급이 타임아웃나면 이 Mac 의
공인 IP 미등록입니다:

```bash
curl -s -o /dev/null -w '%{http_code}\n' "$ADMIN_API_URL/health"
curl -s https://checkip.amazonaws.com
```

첫 줄 `200` 이어야 하고, 아니면 둘째 줄의 IP 를 운영자에게 보내 등록을
요청하십시오.

### 절차 2. credential helper 작성

Codex 가 토큰이 필요할 때마다(`refresh_interval_ms` 주기 + 401 재시도 시)
실행해 VK 한 줄을 받는 스크립트입니다. **Cowork 절차 2 와 같은 파일**입니다 —
이미 `/usr/local/bin/llm-gateway-helper.sh` 가 있으면 이 절 생략.

**① 값 확인** — ▶ 🔵 `echo "$OIDC_ISSUER_URL"` `echo "$OIDC_CLIENT_ID"`
`echo "$ADMIN_API_URL"` 세 값이 찍혀야 합니다. 빈 줄이면 절차 1-③ 의 `export`
를 다시 붙여넣으십시오.

**② 파일 만들기** — ▶ 🔴 (`/usr/local/bin` 에 쓰므로 `sudo` 필요)

```bash
sudo mkdir -p /usr/local/bin
sudo tee /usr/local/bin/llm-gateway-helper.sh >/dev/null <<EOF
#!/bin/bash
set -euo pipefail
export OIDC_ISSUER_URL="$OIDC_ISSUER_URL"
export OIDC_CLIENT_ID="$OIDC_CLIENT_ID"
export ADMIN_API_URL="$ADMIN_API_URL"
export HOME="\${HOME:-/Users/\$(id -un)}"
H="\$HOME/.local/bin/api-key-helper"
[ -x "\$H" ] || H="\$(command -v api-key-helper || true)"
if [ -z "\$H" ]; then
  echo "api-key-helper not found" >&2
  exit 1
fi
"\$H" 2>/dev/null | grep -m1 '^vk-'
EOF
sudo chmod +x /usr/local/bin/llm-gateway-helper.sh
```

`\` 없는 값 세 개는 **지금 창의 값이 파일에 박히고**, `\$` 붙은 것은 실행
시점에 평가됩니다. Codex 는 이 명령의 **stdout 첫 `vk-` 줄**을 bearer 토큰으로
씁니다(공백 trim, 빈 출력은 오류 처리).

**③ 확인** — ▶ 🔵 (⚠️ **`sudo` 붙이지 말 것** — root 기준이 되어 토큰을 못
찾습니다)

```bash
/usr/local/bin/llm-gateway-helper.sh
```

`vk-` 한 줄이면 완료.

### 절차 3. Codex 설치

둘 중 필요한 쪽, 또는 둘 다 설치합니다 — **`~/.codex/config.toml` 을 공유**하므로
절차 4 설정은 한 번이면 됩니다.

**① Codex CLI** — ▶ 🔵 둘 중 하나:

```bash
npm install -g @openai/codex        # Node.js 필요
# 또는
brew install codex                  # Homebrew
```

확인: ▶ 🔵 `codex --version` 이 찍히면 완료.

**② Codex 데스크톱** — ChatGPT macOS 앱(또는 Codex 데스크톱 앱)을 설치합니다.
⚠️ **실측 전**: 데스크톱 앱의 Codex 워크스페이스가 custom provider 사용 시에도
ChatGPT 로그인을 요구하는지는 실기기 확인이 필요합니다 — 로그인 화면이 뜨면
§4 의 판별표를 보십시오.

### 절차 4. `~/.codex/config.toml` 작성

**① 값 확인** — ▶ 🔵

```bash
BASE="$ANTHROPIC_BASE_URL"; echo "$BASE"
```

`https://` 주소가 찍혀야 합니다(빈 줄이면 절차 1-③ 재실행).

**② 설정 파일 생성** — ▶ 🔵 그대로 붙여넣으십시오 (`$BASE` 만 치환됨).
기존 `~/.codex/config.toml` 이 있으면 덮어쓰지 말고 아래 블록을 병합하십시오.

```bash
mkdir -p ~/.codex
cat > ~/.codex/config.toml <<EOF
model = "gpt-5.5"
model_provider = "gateway"

[model_providers.gateway]
name = "US LLM Gateway (Mantle GPT-5.5)"
base_url = "$BASE/v1"
wire_api = "responses"
requires_openai_auth = false

[model_providers.gateway.http_headers]
originator = "codex_desktop"

[model_providers.gateway.auth]
command = "/usr/local/bin/llm-gateway-helper.sh"
timeout_ms = 10000
refresh_interval_ms = 300000
EOF
cat ~/.codex/config.toml
```

키 설명:

- **`wire_api = "responses"`** — 필수. 게이트웨이의 codex 경로는 OpenAI
  Responses API(`/v1/responses`)만 받습니다.
- **`auth.command`** — Codex 가 토큰이 필요할 때마다 helper 를 실행해 VK 를
  받습니다. **`env_key` 와 동시 사용 불가.**
- **`refresh_interval_ms = 300000`** — 5분마다 선제 갱신(VK 는 1시간 유효).
  `0` 이면 401 재시도 때만 갱신합니다.
- **`http_headers.originator`** — **데스크톱 필수.** CLI 는 자체적으로
  `originator: codex_exec` 를 보내 `client=codex` 로 식별되지만, 데스크톱
  앱은 codex 계열 originator 를 보내지 않아 `client=other` 로 분류돼
  403 이 납니다(실측). `codex_desktop` 은 `startswith("codex")` 판정을
  통과해 CLI·앱 모두 정상 분류됩니다 — CLI 사용만 한다면 생략 가능.
- **`model`** — `model = "gpt-5.5"` (실제 OpenAI 모델명)를 권장합니다.
  Codex 내장 메타데이터(context window 등) 테이블에 있어 경고 없이
  동작하고, 요청 모델은 어차피 **routing_profile 의 `default_model`**
  로 라우팅됩니다 — ✅ 실측: 다른 ACTIVE alias (`codex-gpt-6.1-sol` 등)를
  `model` 로 지정해도 응답은 default(`gpt-5.6-luna` 경유)로 내려왔습니다.
  즉 이 배포에서 `model` 값은 기록·식별용이지 모델 선택에는 영향이
  없습니다. 사용 가능한 모델은 라우팅 프로필의 default 를 바꾸는 방식으로만
  바뀝니다(운영자 영역). ⚠️ alias 중 `gpt-6.1-sol` 처럼 INACTIVE 인 것은
  데이터 보존(data retention) 정책으로 의도적으로 비활성화된 것입니다 —
  선택 불가 모델이니 admin UI 에서 활성화하지 마십시오.

> ⚠️ **Bedrock 데이터 보존 모드와 OpenAI 모델** — AWS 계정/리전의
> `data_retention_mode` 가 **`none`(제로 데이터 보존: AWS·provider 어디에도
> 보존 안 함)이면 OpenAI 모델 호출이 차단**됩니다. OpenAI 모델은 AWS 측
> 안전/오용 방지 보존을 요구하므로 `none` 에서는 runtime 엔드포인트가
> `ValidationException`, mantle 엔드포인트는 모델을 unavailable 로 표시합니다.
> **`default` 모드에서는 사용 가능**합니다(모델의 기본 보존 정책 적용 — AWS 가
> 안전 목적으로 보존할 수 있으나 provider 에게는 전달되지 않음). 모드 순서는
> `none < default < aws_review < provider_data_share` 이며, 모델이 요구하는
> 최소 모드 이상일 때만 호출됩니다. 따라서 OpenAI alias(`gpt-*`,
> `codex-gpt-*`)를 쓰려면 계정 보존 모드를 `default` 이상으로 둬야 하고,
> `none` 정책을 유지하는 환경에서는 OpenAI 모델 자체가 선택지가 아닙니다.
> 참고: [Amazon Bedrock — Data retention](https://docs.aws.amazon.com/bedrock/latest/userguide/data-retention.html)

⚠️ **데스크톱 앱 config**: 앱이 `config.toml` 검증을 CLI 보다 엄격하게 하는
사례가 있습니다(`amazon-bedrock` provider 의 `auth`/`base_url` 거부 —
앱 버전 26.721 에서 해결). 본 문서의 `auth.command` + `http_headers` 구성은
실측 통과했지만, 향후 앱 업데이트로 **CLI 는 되는데 데스크톱만 거부**하는
증상이 새로 나오면 앱 업데이트 후 재시도하거나 CLI 사용으로 우회하십시오.

### 절차 5. 실행 및 검증

1. **CLI**: ▶ 🔵 `codex "hi"` (또는 `codex exec "hi"`) — 응답이 오면
   Mac→게이트웨이→백엔드 종단 통과. ⚠️ 게이트웨이 변경 직후엔
   **라우팅 캐시 5분** 대기(그 전엔 404 가능).
2. **데스크톱**: 앱에서 Codex 스레드를 열어 짧은 요청 — 같은 경로를 탑니다.
   provider/auth 를 바꿨으면 앱 완전 종료(Cmd+Q) 후 재실행. ⚠️ 데스크톱은
   절차 4 의 `http_headers.originator` 가 없으면 `403 Client 'other'` 가
   납니다 — 앱 자체는 codex 계열 originator 를 보내지 않습니다(실측).
3. **게이트웨이 쪽**: 운영자가(🟢) `bash 04-verify.sh` → C 섹션 최근 행
   `client=codex`, `status=SUCCESS`, model 은 라우팅 프로필의 default alias
   로 기록돼야 합니다. 게이트웨이는 `originator` 헤더가 codex 접두어인지로
   `client=codex` 를 식별합니다 — CLI 는 `codex_exec`/`codex_cli_rs` 를 자동
   전송하고, 데스크톱은 위 `http_headers` 설정값(`codex_desktop`)이 나갑니다.

---

## 4. 문제 판별

| 증상 | 원인 |
| --- | --- |
| `401` / 인증 실패 | helper 가 `vk-` 를 못 뱉음 → 절차 2-③ 직접 실행으로 판별 |
| helper 는 되는데 Codex 만 인증 실패 | `auth.command` 경로 오타·실행권한 없음 → `sudo chmod +x` |
| `403 Model not allowed` | 사용자/팀 `allowed_models` 에 default alias 미포함 → admin UI 에서 추가 |
| `404` / 모델 없음 | default alias INACTIVE, 또는 라우팅 캐시 5분 미경과 |
| `ValidationException` / OpenAI 모델 unavailable | 계정 데이터 보존 모드 `none` — OpenAI 모델은 `default` 이상 필요 (절차 4 ⚠️) |
| "Model metadata not found" 경고 | `model` 에 게이트웨이 alias 를 넣음 — 의도한 모델 선택이면 무시 가능, 아니면 `gpt-5.5` 로 교체 (절차 4) |
| config 를 썼는데 적용이 안 됨 | 프로젝트 `.codex/config.toml` 에 넣음 → 반드시 `~/.codex/config.toml` (절차 2) |
| 데스크톱만 `403 Client 'other' not allowed` (CLI 는 정상) | 앱이 codex 계열 originator 를 안 보냄 → 절차 4 의 `[model_providers.gateway.http_headers]` `originator` 확인 |
| 데스크톱만 거부 (403 이외) | 앱의 config 검증 — 앱 업데이트 후 재시도, 또는 CLI 우회 (절차 4 ⚠️) |
| VK 발급 타임아웃 (로그인은 성공) | 이 Mac 공인 IP 가 `inbound-cidrs` 에 없음 → `05-allow-client-ip.sh` |
| `refresh failed: HTTP 400` | refresh token 만료 → `gateway-cli login` 재실행 |
| `uv: command not found` | PATH 미반영 → 새 터미널, 그래도 안 되면 설치 스크립트 재실행 (절차 1-⓪) |
| 특정 시점부터 전 요청 인증 실패 | refresh token 만료로 helper 가 빈 출력 → `gateway-cli login` 재실행 |
| 전 요청 502 | 게이트웨이 앞단 ingress 경로 미개방 — CloudFront 쓰는 배포면 `03 --allow-cloudfront`, ALB 직결이면 SG/inbound 규칙 확인 |
| `codex exec` 는 되는데 대화형만 이상 | originator 가 `codex_exec` vs `codex_cli_rs` — 둘 다 `codex` 접두어라 정상 |

---

## 5. 검증 기록

**Codex CLI·데스크톱 — macOS 실기기 종단 검증 완료.** `auth.command` helper 가
`vk-` 를 발급하고 `wire_api="responses"` 호출이 라우팅 프로필의
`default_model`(현 배포: `gpt-5.6-luna` 런타임 plane, `us.openai.gpt-5.6-luna`
응답 확인)로 종단 응답까지 도달함을 확인했습니다.

**CLI — 검증됨** (2026-10-09, codex-cli 0.162.0-alpha, ChatGPT.app 번들):

- [x] `auth.command` 가 CLI 에서 토큰을 정상 발급 (`vk-` bearer)
- [x] `codex` 로 `/v1/responses` 종단 호출 성공 — `originator: codex_exec` 헤더
  로 `client=codex` 식별 확인 (`/admin/dashboard/client-share` 에 codex 행 기록)
- [x] 요청 `model` 과 무관하게 라우팅 프로필의 `default_model` 로만 전달됨을
  확인 — `model="gpt-6.1-sol"`(INACTIVE)·`model="codex-gpt-6.1-sol"`(ACTIVE)
  요청 모두 응답 `model=us.openai.gpt-5.6-luna`. `usage_logs` 도 같은 alias 로
  적재
- [ ] `refresh_interval_ms` 주기 갱신 동작 (장시간 사용 중 확인 필요) —
  ⚠️ refresh token 7일 만료 시 helper 가 무소음 `exit 1` → `gateway-cli login`
  재실행 (§4 판별표 `refresh failed: HTTP 400` 행)

**데스크톱 앱 — 검증됨** (2026-10-09, ChatGPT.app 번들 Codex):

- [x] custom provider `auth.command` 수용 — `vk-` 발급·호출 정상
- [x] ⚠️ 앱은 codex 계열 `originator` 를 **자동으로 보내지 않음** — 무설정 시
  `client=other` → `403 Client 'other' not allowed` (앱 로그
  `~/Library/Logs/com.openai.codex/` 에서 확인). `config.toml` 의
  `[model_providers.gateway.http_headers] originator = "codex_desktop"` 으로
  해결 — 정적 헤더 주입이 실제 요청에 실리는 것을 로컬 캡처로 확인 후,
  실 게이트웨이에서 종단 호출 성공 (`Hi! How can I help?`, 14,052 tokens)
- [x] Codex 워크스페이스는 별도 ChatGPT 로그인 없이 custom provider 경로 사용
- [ ] 데스크톱의 모델 목록/선택 UI 가 게이트웨이 alias 와 어떻게 상호작용하는지
  (default_model 만 실제 적용되므로 선택 UI 의 의미는 제한적)

---

## 6. 조직 배포·참고

**조직 배포** — helper 파일(`/usr/local/bin/llm-gateway-helper.sh`)은 경로가
사용자명과 무관해 MDM 스크립트/패키지로 그대로 밀어넣을 수 있습니다.
`~/.codex/config.toml` 은 사용자 홈 아래 파일이라 MDM 프로파일로는 못 넣고,
설정 스크립트로 배포합니다(사용자 컨텍스트에서 실행 — root 로 쓰면
`/var/root/.codex` 에 들어갑니다).

**대안: 격리 컨테이너** — 호스트의 `~/.codex`·셸 설정을 전혀 안 건드리는 방식이
필요하면 `gateway-clients/` 의 codex-box(`./gw.sh codex`)를 씁니다. 단, 이
방식은 `GATEWAY_VK` 를 env 로 주입하는 **수동 갱신** 방식이라(1시간 유효,
`./gw.sh vk` 재실행) 상시 사용은 본 문서의 `auth.command` 방식이 낫습니다.

**공식 문서** (값이 어긋나면 아래가 정본):

- [Codex Configuration Reference](https://developers.openai.com/codex/config-reference) — `model_providers`, `auth.command`, `refresh_interval_ms`
- [Codex Advanced Configuration](https://developers.openai.com/codex/config-advanced) — command-backed provider auth 상세
