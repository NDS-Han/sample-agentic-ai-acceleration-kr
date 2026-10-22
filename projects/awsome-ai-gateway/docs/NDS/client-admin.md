# 클라이언트 배포·프로비저닝 — 관리자용

> **누가**: 게이트웨이 배포·인스톨러 빌드·직원 프로비저닝 담당자.
> 직원(앤드유저)용 절차는 [client-setup.md](client-setup.md) — 이 문서는
> 그 문서가 "운영자에게 받으세요"라고 부르는 것들을 **만드는 쪽**의 절차다.

역할 분담 요약:

| | 앤드유저가 하는 것 | 관리자가 하는 것 |
|---|---|---|
| 인증 | `gateway-cli login` (브라우저 OIDC) | Cognito 사용자·그룹 생성 ([operations.md §1](operations.md)) |
| 설정 | setup/config.toml 1회 | site-config 베이크 + 인스톨러 빌드·배포 |
| 네트워크 | 없음 | `network.allowed_cidrs` + SG 에 직원 IP 등록 |
| 진단 | 증상 보고 | `usage_logs`·라우팅 프로필·VK scope 확인 |

---

## 1. 직원에게 넘길 것 — env 4줄 + 계정 + IP 허용

```bash
./deploy client-values    # env 4줄 출력 — 이 블록을 그대로 직원에게 전달
```

| 값 | 출처 |
|---|---|
| `OIDC_ISSUER_URL` / `OIDC_CLIENT_ID` | `gateway.yaml` `oidc:` → 없으면 terraform output `cognito_*` |
| `ANTHROPIC_BASE_URL` / `ADMIN_API_URL` | eks: values ingress host / ecs: `alb_dns_name` / compose+도메인: `gateway.<domain>` / 무도메인: 공인 IP:8000 |

- **계정**: Cognito 사용자 생성 + `Claude_<dept>_<team>` 그룹 배정 — [operations.md §1](operations.md).
  팀 그룹이 없으면 로그인은 되는데 **VK 발급이 403** — 직원이 가장 먼저 만나는 함정.
- **IP 허용**: `gateway.yaml` `network.allowed_cidrs` 에 직원 대역 추가 후 `./deploy apply`.
  admin-api·게이트웨이 둘 다 IP로 막히므로, 빠뜨리면 "로그인은 되는데 키 발급·추론만 타임아웃".
- **https**: Cowork 는 `https://` 필수 — `domain.mode`가 `none`이면 Cowork 사용자는 제외 안내.

---

## 2. 설치물 빌드 — 직원에게 "명령어 대신 파일"을 주고 싶을 때

직원이 repo 클론 + uv/pip 설치를 못 하는 경우(잠긴 PC, 비개발 직군) 빌드 산출물을 배포합니다.
세 가지 빌드물이 있고 용도가 다릅니다:

| 빌드물 | 소스 | 산출 | 대상 |
|---|---|---|---|
| **gateway-cli suite (Windows)** | `installer/packaging/` | `gateway-cli-setup-<v>.exe` (Inno Setup) | Claude Code·Codex용 Windows 직원 |
| **gateway-cli suite (Linux/WSL)** | `installer/packaging-linux/` | `gateway-cli-setup-<v>.run` 또는 `.deb` | Linux·WSL 직원 |
| **cowork 인스톨러 (Windows)** | `cowork-installer/installer/packaging/` | `gateway-cli-cowork` 설치 exe | Claude Desktop 사용자 — HKLM 정책까지 자동 기록 |

### 공통 빌드 절차

```bash
# ① site-config.json.example 을 복사해 site-config.json 으로 채움 (gitignore 처리됨 — 커밋 금지)
#    8키: oidcIssuerUrl/oidcClientId/gatewayUrl/adminApiUrl/caBundle
#         + 프록시 검증 expectedProxyUrl/noProxyValue/forbiddenNoProxyToken
cp installer/packaging/site-config.json.example installer/packaging/site-config.json
$EDITOR installer/packaging/site-config.json

# ② 빌드 — Windows 산출물은 Windows 머신에서, Linux 산출물은 Linux 머신에서
#    (PyInstaller 는 크로스컴파일 안 됨)
powershell -File installer\packaging\build.ps1 -SignThumbprint <인증서>   # Windows
./installer/packaging-linux/build.sh --sign-gpg-key <keyid>              # Linux .run
./installer/packaging-linux/deb/build-deb.sh                             # Linux .deb
```

- **서명은 프로덕션 필수** — unsigned PyInstaller exe 는 SmartScreen/AV 에 걸립니다
  (Windows: `-SignThumbprint` 또는 `-SignPfxFile`, Linux: `--sign-gpg-key` + 자동 `.sha256`).
- **cowork 인스톨러**는 별도 빌드 — `cowork-installer/installer/packaging/build.ps1`,
  site-config 에 cowork 전용 키(`coworkGatewayHttpsUrl`, `orgUuid`, `expectedCaSha256`,
  `coworkModels`) 추가. 상세는 `us-llm-gateway/cowork/installer/cowork-installer-build-windows.md`.
- site-config 없이 빌드하면 OIDC 값이 비어 직원이 `--issuer-url` 등을 손으로 넘겨야 합니다.
- 오프라인 빌드: `download_wheels.ps1`/`.sh`로 wheel 캐시를 먼저 만들고 `-WheelDir`/`--wheel-dir`.

### 배포

사내 파일 공유·MDM·직접 전달 아무 데나 — 단 **`.sha256` 체크섬 파일을 같이 게시**하고
직원에게 검증 방법을 알려주세요 (`Get-FileHash` / `sha256sum -c`).

---

## 3. 클라이언트별 사전 조건 — "로그인은 되는데 안 됨"의 서버측 원인

| 클라이언트 | 필요한 서버 설정 | 빠뜨리면 |
|---|---|---|
| Claude Code | routing profile `client='claude-code'` enabled + claude-* alias ACTIVE + 키 `allowed_models` 포함 | `/status`는 되는데 `403 Model not allowed` |
| Cowork | 동일 + **`https` 게이트웨이 URL** + `inferenceModels` 로스터에 있는 alias들 ACTIVE | 앱이 모델 목록을 못 띄우거나 403 |
| Codex | routing profile `client='codex'` enabled + codex용 alias(예 `gpt-6.1-sol`) ACTIVE + 키 scope | 추론 403 / 모델 폴백 |

주의 — **client 분류는 originator 헤더로 결정**됩니다. Codex Desktop 앱은 codex 헤더를
자동으로 안 보내므로 config.toml 의 `http_headers.originator="codex_desktop"`이 없으면
`client='other'`로 분류돼 codex 프로필이 아니라 다른 규칙을 탑니다(보통 403).
직원이 "codex가 403"이라 하면 usage_logs 의 `client` 값부터 확인.

---

## 4. 서버측 진단 — 직원이 "안 된다"고 할 때 보는 곳

| 직원 증상 | 서버에서 확인할 것 |
|---|---|
| `no group mapping found` | Cognito 그룹 배정 누락 → operations.md §1 (팀 그룹 필수) |
| 로그인 OK, VK 발급 실패·타임아웃 | 직원 공인 IP가 `allowed_cidrs`/SG 밖 — Cognito(공개)는 통과, admin-api(차단)에서 막힘 |
| `403 Model not allowed` | VK `allowed_models` scope 또는 routing profile client/alias 상태 |
| 사용량이 `client=other`로 잡힘 | Codex Desktop: `originator` 헤더 누락 → config.toml §2-C 블록 재적용 |
| 의도한 모델이 아닌 모델로 과금됨 | 서버 로그 `responses_requested_model_unresolved_using_default` — 미등록/INACTIVE alias 요청이 default_model로 조용 폴백된 것 |
| Cowork helper 미호출 | 직원 프로필이 Local 레이어 — 관리형(.mobileconfig/HKLM) 재설치 필요 |

---

## 5. 버전업 · 재배포

1. `gateway-cli-v2/pyproject.toml` 버전 bump → 재빌드(§2) → 새 exe/.run/.deb 배포.
2. 직원 쪽은 재설치 — Windows는 같은 AppId 라 인스톨러가 업그레이드로 처리,
   Linux는 `.run` 재실행(덮어쓰기) 또는 `apt`/`dpkg`가 버전 비교.
3. 토큰 캐시·설정은 설치물 교체와 무관하게 `~/.gateway-cli`/`~/.codex` 등에 남습니다.

> ⚠️ **포크 드리프트 주의** — `installer/` 와 `cowork-installer/`는 원래 같은 코드의
> 두 복사본입니다. 한쪽의 보안/버그 수정이 다른 쪽에 자동 반영되지 않습니다(실제로
> R26 수정이 cowork 쪽에 누락됐다가 R32 에서 백포트). 클라이언트 코드를 고칠 때는
> 양쪽 트리를 같이 확인하고, 릴리스 전 `installer/packaging` ↔
> `cowork-installer/installer/packaging` 의 공유 파일 diff를 훑으세요.

---

## 6. 관련 문서

| 문서 | 내용 |
|---|---|
| [client-setup.md](client-setup.md) | 앤드유저용 — 이 문서의 산출물(env 4줄·인스톨러)을 쓰는 절차 |
| [operations.md](operations.md) | Cognito 사용자·그룹, 오픈 전 하드닝 체크리스트, 라우팅 프로필 SQL |
| `installer/packaging/README.md` | Windows 빌드 상세 (서명·site-config·site-extra·오프라인) |
| `installer/packaging-linux/README.md` | Linux 빌드 상세 (.run/.deb·GPG·wheel 캐시) |
| `us-llm-gateway/cowork/installer/` | cowork 인스톨러 빌드·사용·제거 상세 |
