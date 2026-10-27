# Cowork(Claude Desktop 3P) 클라이언트 설치

> **상태: 종단 검증 완료 — 2026-08-03, Windows Server 2025.**
> 절차 1~5 를 처음부터 끝까지 실제로 돌려 응답과 웹서치까지 확인했고, 그 과정에서 드러난 문서 문제를 반영했습니다. 검증 내용은 맨 아래 「검증 기록」에 있습니다.

Claude Code 가 아니라 **Cowork(Claude Desktop 3P)** 를 게이트웨이에 붙이는 문서입니다. 게이트웨이 쪽 준비(../README.md의 NDS 배포 절차)가 끝난 뒤에 하는 작업입니다. **macOS 는** 같은 폴더의 `cowork-client-install-macos.md` **를 보십시오.**

> **독자**: Windows PC에 Cowork를 손으로 붙이는 직원/관리자.
> **전제**: 게이트웨이 https 도메인 + 본인 VK + 관리자 권한 PowerShell.
> **결과**: Cowork가 게이트웨이로 호출하고 대시보드에 `client=cowork`로 기록됨.
>
> **길찾기** — 전부 읽을 필요 없이 상황에 맞는 절만:
>
> | 상황 | 볼 절 |
> |---|---|
> | 처음 설치 | §1~§4 순서대로 (절차 0~5) |
> | 안 되는데 이유를 모르겠음 | §5 문제 판별 |
> | 직원 여러 명에게 배포 | §6 조직 배포 → 인스톨러 경로 |
> | 앱 기능 토글 | §7 |
> | 문서 신뢰성 확인 | §8 검증 기록 |

---

## 1. 한눈에

Cowork를 회사 게이트웨이에 연결합니다. 연결 후 앱은 claude.ai 대신 게이트웨이로 가고, 키는 헬퍼가 자동 발급합니다.

```
실행 가능 확인 → gateway-cli 설치·로그인 → helper 작성 → 앱 설치 → 관리형 설정 → 실행 확인
    절차 0             절차 1              절차 2       절차 3      절차 4       절차 5
```

| 절차 | 내용 | 창 | 완료 신호 |
|---|---|---|---|
| 0 | Cowork 실행 가능 점검 (2MB 도구) | — | `ready for Cowork` |
| 1 | gateway-cli 설치 + 회사 계정 로그인 | 🔵 | `api-key-helper`가 `vk-` 출력 |
| 2 | helper 스크립트 작성 | 🔵→🔴→🔵 | 직접 실행 시 `vk-` 한 줄 |
| 3 | Cowork `.msix` 설치 (전 사용자) | 🔴 | `Get-AppxPackage` 출력 |
| 4 | 관리형 설정 기록 (HKLM `inference*` 6개) | 🔴 | `Get-ItemProperty` 출력 |
| 5 | 앱 실행 → 짧은 대화 → 게이트웨이 기록 확인 | — | `usage_logs`에 `client=cowork` |

🔵 일반 PowerShell · 🔴 관리자 PowerShell — 창 구분 표는 §4 앞 표 참조.

**운영자에게 미리 받을 것**

| 받을 것 | 출처 |
|---|---|
| env 값 4개 | 운영자가 `./deploy client-values` 출력 |
| 로그인 계정 | 이메일 + 임시 비밀번호 (첫 로그인 시 재설정) |
| 이 PC 공인 IP 등록 | 운영자가 inbound-cidrs에 미리 등록 |

⚠️ **순서 고정** — 절차 4(설정) 전에 앱을 켜면 claude.ai 로그인 화면이 떠 개인 계정으로 들어갈 수 있습니다.
⏱️ 소요: 다운로드(~1.8GB) 제외 약 30분 (실측).

> 로그인 방식이 사내 통합 로그인으로 바뀌어도 절차는 동일 — `OIDC_ISSUER_URL`/`OIDC_CLIENT_ID` 값만 달라집니다.

---

## 2. 전제

**게이트웨이 쪽** — ../README.md의 NDS 배포 절차 완료:

| 필요 | 확인 경로 |
|---|---|
| `https://` 도메인 | ../domain.md (Cowork는 https 필수) |
| Cowork 라우팅 프로필 | ../operations.md §3 또는 /apps UI |
| 모델 alias ACTIVE | 관리 UI /models |
| 클라이언트 공인 IP ∈ inbound-cidrs | gateway.yaml → apply |

IP 등록이 빠지면 **로그인은 되는데 VK 발급만 타임아웃** — Cognito는 공개, admin-api는 IP 제한이라 증상이 갈립니다.

**클라이언트 쪽** — `gateway-cli` + 로그인 토큰 (절차 1). Claude Code를 이미 쓰는 PC면 로그인 재사용 → 절차 1 생략.

⚠️ `gateway-cli`는 **이 저장소(fork)에서** 설치 — upstream은 managed-settings 경로 버그가 남아 있어 조용히 게이트웨이를 우회합니다.
⚠️ **하드웨어 가상화 필수** — 일반 EC2에서는 Cowork 미지원 (`windows-test-machine-setup.md`).

**방화벽** — 사내망이면 시작 전 네트워크 담당자와 조율:

| 호스트 | 용도 | 막히면 |
|---|---|---|
| 게이트웨이 `https://` URL | 추론 전부 | 응답 없음 |
| `OIDC_ISSUER_URL` | 로그인·VK 갱신 | 로그인 실패 |
| `claude.ai` | 절차 0·3 파일 다운로드 | 다운로드 실패 (설치 후 불필요) |
| `downloads.claude.ai` | **Cowork 세션 시작 시** 작업환경 번들 (offline판은 내장) | 앱은 뜨는데 **세션만 안 열림** — 가장 헷갈리는 실패 |
| `releases.claude.com` | 자동 업데이트 | 업데이트 정지 (사용 무관) |

폐쇄망이면 절차 3의 offline 설치판 사용 + §7 자동 업데이트 끄기. 조직 배포는 앱 설정 창의 **Egress Requirements**로 호스트 목록을 `.txt`로 내보내 사용 (§6). 전체 목록: [required egress](https://claude.com/docs/third-party/claude-desktop/telemetry#required-egress-paths).

---

## 3. 먼저 정할 것 세 가지

독립적인 설치 축 세 개 — 이 배포의 선택은:

| 축 | 선택지 | 선택 |
|---|---|---|
| ① 앱 설치 범위 | per-user / **per-machine** | **per-machine** |
| ② 설정 소스 | Local / **관리형** | **관리형** (아래 이유) |
| ③ 관리형 전달 | 수동 / **MDM** / Bootstrap | 테스트=수동, 배포=MDM |

- **②는 선택이 아님**: `inferenceCredentialHelper`는 공식 **MDM-Only 키** — Local에 넣으면 조용히 무시됩니다(helper는 실행되는데 출력만 안 먹히는 함정). 관리형 = `HKLM\SOFTWARE\Policies\Claude`. → [공식 문서](https://claude.com/docs/third-party/claude-desktop/configuration#inferencecredentialhelper)
- 앱 설정창의 `Apply locally`는 Local에 쓰기, `Export`는 배포용 `.reg`/`.mobileconfig` 생성 — 소스는 둘뿐입니다.
- **Bootstrap 불필요**: 사용자별 자격증명은 helper가 해결하고, helper 키는 bootstrap에 실을 수도 없습니다.

---



## 4. 절차

명령 블록 앞에는 **어느 창에서 돌리는지** 표시가 붙습니다. 창을 잘못 고르면 오류 없이 조용히 빗나가므로, 매번 확인하십시오.


| 표시                           | 어느 창인가                                           | 왜 나뉘나                              |
| ---------------------------- | ------------------------------------------------ | ---------------------------------- |
| ▶ 🔵 **실행 · 일반 PowerShell**  | 직원 본인 계정으로 그냥 연 PowerShell                       | 설치 파일과 로그인 토큰이 **직원 본인 폴더**에 들어갑니다 |
| ▶ 🔴 **실행 · 관리자 PowerShell** | **"관리자 권한으로 실행"** 으로 연 창 (제목 표시줄에 `관리자:` 가 보입니다) | 모든 사용자에게 앱을 깔고 시스템 영역에 설정을 씁니다     |
| ▶ 🟢 **실행 · 배포 EC2**         | 직원 PC 가 아니라 **운영자**가 게이트웨이 서버에서                  | 직원이 볼 수 없는 값을 뽑습니다                 |


⚠️ 🔵 자리에서 🔴 창을 쓰면 **관리자 계정의 폴더**에 설치되고 토큰도 그쪽에 저장됩니다. 명령은 성공한 것처럼 보이는데 Cowork 는 아무것도 못 찾습니다.

### 절차 0. Cowork 실행 가능 여부 확인

**이걸 먼저 하십시오.** Cowork 는 작업을 격리된 가상 환경에서 돌리기 때문에, PC 가 그걸 지원해야 합니다. 안 되는 PC 라면 아래 절차를 다 마쳐도 앱이 안 켜집니다 — 확인이 2 MB·1분이니 먼저 거르는 편이 낫습니다.

**브라우저**로 이 주소를 열어 점검 도구를 받으십시오(약 2 MB).

```
https://claude.ai/api/desktop/win32/x64/cowork-readiness-check/latest/redirect
```

**Arm 프로세서 PC**(Snapdragon 계열 노트북)라면 이 주소입니다. 어느 쪽인지 모르면 `설정 → 시스템 → 정보`의 「시스템 종류」를 보십시오.

```
https://claude.ai/api/desktop/win32/arm64/cowork-readiness-check/latest/redirect
```

⚠️ `Invoke-WebRequest` 로는 못 받습니다(403). 절차 3 의 앱 설치 파일과 같은 제약입니다 — **실제 브라우저로만** 받힙니다.

받은 파일을 실행하면 항목별로 통과 여부가 나옵니다.

```
This computer is ready for Cowork          ← 이 문장이 나오면 다음으로
This computer does not meet the requirements for Cowork
```

#### 통과하지 못했다면

도구가 **어느 항목이 왜 막혔는지와 고치는 명령까지** 알려줍니다. 자주 걸리는 둘입니다.

```
Hardware virtualization    펌웨어(BIOS/UEFI)에서 가상화가 꺼져 있음
Virtual Machine Platform   Windows 기능이 안 켜져 있음
```

**고친 뒤에는 반드시 재부팅하고 다시 실행하십시오.** 기능을 켜기만 하고 확인하면 여전히 실패로 나옵니다 — 실측에서 재부팅 한 번으로 전 항목 통과로 바뀌었습니다.

⚠️ **Windows Server 에서는 도구의 안내만으로 부족합니다.** 도구는 Windows 10·11 기준으로 처방을 알려주는데, Server 는 `VirtualMachinePlatform` 만 켜서는 필요한 서비스가 안 깔립니다. **Hyper-V 역할**을 설치해야 합니다. 직원 PC 가 아니라 서버에 시연·검증 환경을 만드는 경우에만 해당합니다.

⚠️ **가상 머신 위에서는 대개 실패합니다.** 일반 클라우드 인스턴스처럼 가상화 기능을 손님에게 넘겨주지 않는 환경에서는 통과할 수 없습니다.

> 📖 [기기 요구사항과 점검 도구](https://claude.com/docs/third-party/claude-desktop/installation#check-device-readiness) — macOS 용 점검 도구 주소도 여기 있습니다.

### 절차 1. `gateway-cli` 설치 + 로그인

Cowork는 VK를 직접 만들지 못하므로 `api-key-helper`가 대신 받아옵니다.

> Claude Code를 이미 쓰는 PC면 **이 절 생략** — 토큰(`%USERPROFILE%\.gateway-cli\`)을 공유합니다.

이 절은 **🔵 일반 PowerShell**에서 진행합니다 — 설치·토큰 모두 본인 계정 폴더에 들어가므로 관리자 창에서 하면 관리자 계정에 설치됩니다.

#### ⓪ 사전 요구사항 — 없을 때만

▶ 🔵

```powershell
py --version     # 3.11+ 가 찍히면 건너뜀
git --version
```

⚠️ Python은 **python.org/winget 판** — Microsoft Store 판은 `py` 런처·`--user` 설치가 꼬입니다.
설치 방법은 `winget --version` 결과로 갈림 — 나오면 A, `인식할 수 없습니다`면 B.

##### A. `winget` — 대부분 이쪽

▶ 🔴

```powershell
winget install --id Python.Python.3.12 -e --scope machine
winget install --id Git.Git -e
```

##### B. `winget` 없음 — 설치 파일 직접

▶ 🔵

```powershell
[Net.ServicePointManager]::SecurityProtocol = "Tls12"
$py="https://www.python.org/ftp/python/3.12.8/python-3.12.8-amd64.exe"
$g="https://github.com/git-for-windows/git/releases/download/v2.47.1.windows.1/Git-2.47.1-64-bit.exe"
Invoke-WebRequest -Uri $py -OutFile "$env:TEMP\py.exe"  -UseBasicParsing
Invoke-WebRequest -Uri $g  -OutFile "$env:TEMP\git.exe" -UseBasicParsing
```

▶ 🔴

```powershell
Start-Process "$env:TEMP\py.exe" -Wait -ArgumentList `
  "/quiet InstallAllUsers=1 PrependPath=1 Include_launcher=1"
Start-Process "$env:TEMP\git.exe" -Wait -ArgumentList "/VERYSILENT /NORESTART"
```

`Include_launcher=1` 필수 — `py`를 설치합니다.

**A/B 공통**: 끝나면 **PowerShell 창을 닫고 새로 열어** `py --version`·`git --version` 확인.
⚠️ 창을 새로 안 열면 `py`는 되는데 `git`만 `인식할 수 없습니다`로 나옵니다 — git은 새로 추가된 `C:\Program Files\Git\cmd`에 있어 PATH 반영이 새 창부터입니다(실측).

#### ① 저장소 — fork 에서

⚠️ **반드시 이 저장소(fork)** — upstream은 managed-settings 경로 버그로 조용히 게이트웨이를 우회합니다.

▶ 🔵

```powershell
cd ~
git clone -b nds/dev `
  https://github.com/NDS-Han/sample-agentic-ai-acceleration-kr.git
cd ~\sample-agentic-ai-acceleration-kr\projects\awsome-ai-gateway
```

설치 후에는 지워도 됩니다 — 설치·로그인 스크립트 실행용입니다.

#### ② `gateway-cli` 설치

▶ 🔵

```powershell
py -m pip install --user .\gateway-cli
$s = py -c "import sysconfig; print(sysconfig.get_path('scripts','nt_user'))"
$env:PATH = "$s;$env:PATH"
[Environment]::SetEnvironmentVariable("PATH", "$s;" +
  [Environment]::GetEnvironmentVariable("PATH","User"), "User")
gateway-cli version
```

`pip --user`는 PATH에 없는 폴더에 설치하므로 위처럼 등록해야 이름으로 부릅니다.



#### ③ 운영자에게 받은 값 4개

직원이 직접 찾을 수 없는 값이라 운영자가 배포 머신에서 출력해 전달합니다.

▶ 🟢 **배포 머신** — 운영자

```bash
cd ~/awsome-ai-gateway/docs/NDS
./deploy client-values     # PowerShell·macOS 양식을 함께 출력
```

▶ 🔵 — 받은 4줄을 그대로

```powershell
$env:OIDC_ISSUER_URL="<from operator>"
$env:OIDC_CLIENT_ID="<from operator>"
$env:ADMIN_API_URL="<from operator>"
$env:ANTHROPIC_BASE_URL="<from operator - https:// 필수>"
```

⚠️ `ANTHROPIC_BASE_URL`은 반드시 `https://` — Cowork가 http를 거부합니다. 로그인 스크립트가 이 주소의 `/health`를 먼저 찌르므로 PC→게이트웨이 경로가 여기서 함께 검증됩니다.

#### ④ 로그인

▶ 🔵 — **③의 env를 넣은 같은 창에서** (env는 그 창에만 살아 있음 — 창을 새로 열면 ③부터 다시)

```powershell
cd ~\sample-agentic-ai-acceleration-kr\projects\awsome-ai-gateway
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass -Force
.\scripts\onboard-windows.ps1
```

브라우저 로그인 화면에 운영자 발급 이메일+비밀번호 입력 (첫 로그인은 재설정 요구). 콜백은 `localhost:8090` — 점유돼 실패하면 등록된 콜백(`8090`/`8091`/`8092`) 중 골라 직접 지정:

```powershell
gateway-cli login --issuer-url $env:OIDC_ISSUER_URL `
  --client-id $env:OIDC_CLIENT_ID --redirect-port 8091
```

#### ⑤ 확인

▶ 🔵

```powershell
api-key-helper 2>$null | Select-String "^vk-"
(iwr "$env:ADMIN_API_URL/health" -TimeoutSec 10).StatusCode   # 200 이어야 함
(irm https://checkip.amazonaws.com).Trim()                   # 막혔으면 이 IP 를 운영자에게
```

`vk-` 한 줄이 나오면 완료. ⚠️ **로그인 성공 ≠ VK 성공** — Cognito는 공개라 되지만 VK 발급(admin-api)은 IP 제한이라 이 PC의 공인 IP가 inbound-cidrs에 없으면 여기서 타임아웃납니다.



### 절차 2. credential helper 작성

앱이 요청마다 실행해 VK를 받아오는 스크립트 — `api-key-helper`를 감싸 `vk-` 한 줄만 출력. 위치는 `C:\ProgramData\llm-gateway\helper.cmd` (전 사용자 읽기 가능).

#### ① helper 가 부를 실행파일의 경로 확인

▶ 🔵 — **반드시 직원 본인의 일반 창에서** (관리자 창이면 관리자 경로가 나와 파일 없음)

```powershell
py -c "import sysconfig,os; print(os.path.join(sysconfig.get_path('scripts','nt_user'),'api-key-helper.exe'))"
```

출력 경로를 복사. **절대경로를 쓰는 이유**: Cowork가 보는 PATH엔 설치 위치가 없을 수 있어 이름만으론 못 찾습니다.

#### ② helper 파일 만들기

▶ 🔴 (`C:\ProgramData` 쓰기 — 관리자 권한)

```powershell
$exe    = "<①에서 복사한 경로>"
$issuer = "<from operator>"
$client = "<from operator>"
$admin  = "<from operator>"
$dir = "C:\ProgramData\llm-gateway"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
@"
@echo off
set OIDC_ISSUER_URL=$issuer
set OIDC_CLIENT_ID=$client
set ADMIN_API_URL=$admin
"$exe" 2>nul | findstr /b "vk-"
"@ | Set-Content -Path "$dir\helper.cmd" -Encoding ASCII
```

`-Encoding ASCII` 필수 — BOM이 붙으면 cmd가 첫 줄을 못 읽습니다.

#### ③ 확인

▶ 🔵 — **일반 창에서** (토큰은 본인 폴더에 있어 관리자 창에선 못 찾음)

```powershell
& "C:\ProgramData\llm-gateway\helper.cmd"
```

`vk-` 한 줄이 나오면 완료. ⚠️ 출력 끝이 개행이어야 앱이 읽습니다 — `findstr`이 개행을 붙이므로 위 형태 그대로 유지.

### 절차 3. Cowork 설치

🔴 **관리자 PowerShell** — 전 사용자 설치+시스템 설정이라 여기서부터 관리자 창. ⚠️ **설정보다 앱을 먼저 켜지 마십시오** — 관리형 설정 없이 켜면 claude.ai 로그인 화면이 뜹니다.

#### ① 설치 파일 받기

**브라우저**로 주소를 열어 `.msix` 다운로드 (~1.8GB). **Invoke-WebRequest는 403**이라 브라우저 필수:

```
x64:  https://claude.ai/api/desktop/win32/x64/offline/latest/redirect
arm64: https://claude.ai/api/desktop/win32/arm64/offline/latest/redirect
```

⚠️ 반드시 **offline 판** — `claude.com/download`의 `.exe`는 Cowork가 빠집니다.
⚠️ **404면 잠시 후 재시도** — 새 버전 직후 offline판이 아직 안 올라간 구간이 있습니다(벤더 명시 동작, 고장 아님). 받은 파일의 **전체 경로**를 메모 — 다음 단계는 관리자 창이라 `내 다운로드`가 안 통합니다.

#### ② 설치

▶ 🔴

```powershell
Add-AppxProvisionedPackage -Online -SkipLicense `
  -PackagePath "<받은 .msix의 전체 경로>"
Get-AppxPackage -AllUsers -Name "*laude*" | Select Name,Version
```

이름·버전이 찍히면 설치 완료. `Add-AppxPackage`가 아닌 이유 — Provisioned는 **모든 사용자**에 등록, Add는 실행 계정만.

#### ③ 거부됐을 때만

"이 앱은 설치할 수 없습니다" 계열 거부(대부분 Windows Server 또는 정책 제한 PC)면:

▶ 🔴

```powershell
New-ItemProperty -Path "HKLM:\SOFTWARE\Policies\Microsoft\Windows\Appx" `
  -Name AllowAllTrustedApps -PropertyType DWord -Value 1 -Force
```

대부분 PC는 불필요 — 회사 중앙 관리 PC면 IT에 문의(정책으로 되돌아감).

### 절차 4. 관리형 설정

🔴 **관리자 PowerShell** (절차 3과 같은 창).

`HKLM\SOFTWARE\Policies\Claude`에 씁니다 — `HKLM`에 값이 있으면 `HKCU`는 통째로 무시됩니다(앱 v1.19367.0 미만은 섞어 읽으므로 구버전이 있으면 한쪽으로 몰고 업그레이드).

##### 함정 — 직접 편집기/MDM 콘솔로 넣을 때 조용히 실패합니다

| 이렇게 하면 | 무슨 일이 나나 |
| --- | --- |
| 값을 **하위 키**에 넣음 | 안 읽힘 — 앱은 `...\Policies\Claude` 바로 아래 값만 봅니다 |
| `REG_EXPAND_SZ` | **최악** — "정책 있음"으로 세어 HKCU를 죽이면서 내용은 못 읽어 설정 0개 상태가 됩니다 |
| `REG_QWORD`/`REG_MULTI_SZ`/`REG_BINARY` | 안 보입니다 |
| 키 이름 오타·빈 문자열 | 그래도 "정책 있음"으로 셉니다 → 설정창 잠김이 제대로 읽혔다는 증거가 **아닙니다** |

아래 명령은 `REG_SZ`로 들어가 안전합니다. 참/거짓·숫자는 `REG_DWORD`도 가능.

▶ 🔴 — 손으로 채우는 건 `$BASE` 한 줄

```powershell
$BASE   = "<from operator - 절차 1-③ 의 ANTHROPIC_BASE_URL 과 같은 https:// URL>"
$MODELS = '["claude-sonnet-5-5","claude-opus-5-5","claude-haiku-4-5-20251001"]'
$BASE
```

`$BASE`가 `https://` 주소를 찍는지 확인 후 그대로 붙여넣기:

```powershell
$K = "HKLM:\SOFTWARE\Policies\Claude"
New-Item -Path $K -Force | Out-Null
Set-ItemProperty $K inferenceProvider           "gateway"
Set-ItemProperty $K inferenceGatewayBaseUrl     $BASE
Set-ItemProperty $K inferenceGatewayAuthScheme  "bearer"
Set-ItemProperty $K inferenceCredentialHelper   "C:\ProgramData\llm-gateway\helper.cmd"
Set-ItemProperty $K inferenceCredentialHelperTtlSec 1800
Set-ItemProperty $K inferenceModels             $MODELS
Get-ItemProperty $K | Format-List inference*
```

6개가 다 보이고 `inferenceGatewayBaseUrl`에 실제 주소가 있으면 완료 — `<from operator>`가 보이면 `$BASE` 미설정이니 채우고 다시 실행.

⚠️ alias는 **DB 등록 문자열 그대로** — Haiku는 `claude-haiku-4-5-20251001`처럼 접미 포함. 한 글자 틀리면 그 모델만 404. 현재 ACTIVE 목록은 관리 UI /models에서 확인.
✅ `inferenceModels`는 JSON 문자열(`REG_SZ`)이 맞습니다 — `REG_MULTI_SZ` 불요(실측 확인).

#### 설정 키 요약

| 키 | 값 |
|---|---|
| `inferenceProvider` | `"gateway"` |
| `inferenceGatewayBaseUrl` | 게이트웨이 `https://` 도메인 |
| `inferenceGatewayAuthScheme` | `"bearer"` |
| `inferenceCredentialHelper` | helper 절대경로 |
| `inferenceCredentialHelperTtlSec` | `1800` |
| `inferenceModels` | ACTIVE alias JSON 배열 문자열 |

전체 키 정의는 [공식 configuration reference](https://claude.com/docs/third-party/claude-desktop/configuration)가 정본입니다.

### 절차 5. 실행 및 검증

시작 메뉴에서 **Claude** 실행.

1. **첫 화면**: claude.ai 로그인 화면이 뜨면 **멈추고 §5로** — 관리형 설정 미인식. 바로 쓸 수 있는 상태면 읽힌 것입니다.
2. **모델 선택기**: `inferenceModels`의 이름들이 보이는지 확인.
3. **`hi` 전송**: 응답이 오면 PC→게이트웨이→Bedrock 전부 통과. (게이트웨이 변경 직후는 캐시 5분 — 신규 모델이 404로 보일 수 있음.)
4. **게이트웨이 확인** — 운영자: `cowork`로 분류 안 되면 라우팅·예산이 섞입니다.

▶ 🟢 **배포 머신**

```bash
cd ~/awsome-ai-gateway/docs/NDS && ./deploy doctor
```

| 열 | 기대값 |
|---|---|
| `client` | `cowork` |
| `status` | `SUCCESS` |
| `cost_usd` | 0 아님 |
| `ws` | 실시간 질문 시 1+ (서버측 웹서치) |

⚠️ 비용 검산 시 **캐시 토큰 포함** — Cowork는 큰 시스템 프롬프트를 캐시로 보내 `input`/`output`만 보면 cost보다 훨씬 작게 나옵니다(단가 오류 아님).

---



## 5. 문제 판별


| 증상                                 | 원인                                                                                     |
| ---------------------------------- | -------------------------------------------------------------------------------------- |
| 앱이 claude.ai 로그인 화면을 띄움            | 관리형 설정이 안 읽힘 (레지스트리 하이브/경로 확인)                                                         |
| 모델 목록이 비었거나 다른 이름              | `inferenceModels` 를 못 읽음 — JSON 형식·따옴표 확인 (절차 4)                                        |
| helper 는 VK 를 뱉는데 앱은 인증 실패         | helper 를 관리형이 아닌 레이어에 넣었거나, 출력이 개행으로 안 끝남                                              |
| 터미널에서는 helper 가 되는데 앱에서만 실패        | 앱이 보는 PATH 에 `api-key-helper` 가 없음 → 절대경로로                                             |
| VK 발급 타임아웃 (로그인은 성공)               | 클라이언트 공인 IP 가 `inbound-cidrs` 에 없음 → gateway.yaml 에 추가 후 apply           |
| `refresh failed: HTTP 400`         | refresh token 만료 → `gateway-cli login` 재실행                                             |
| `pip install` 이 `hatchling` 을 못 받음 | 사내망이 PyPI 를 막음 → 운영자에게 `.whl` 을 받아 `py -m pip install --user <받은경로>\gateway_cli-*.whl` |
| `py` 를 인식할 수 없다고 나옴                | Python 런처 미설치 → python.org 설치 파일을 다시 실행해 `py launcher` 를 체크                            |
| 특정 모델만 404                         | alias 오타, 또는 등록 후 5분 미경과                                                               |
| 전 요청 502                           | 게이트웨이 라우팅 미적용 또는 ALB/Ingress 경로 미개방 → `./deploy doctor` 로 점검        |
| 앱은 켜지고 대화도 되는데 **Cowork 세션만** 시작 안 됨 | `downloads.claude.ai` 가 막힘 → offline 설치판으로 다시 설치 (§2 「망 쪽」)                             |
| 위와 같은데 방화벽은 열려 있음                   | 백신·EDR 이 Cowork 에이전트를 막음 → 아래 「EDR·백신에 막히는 경우」                                          |
| 원인이 안 보일 때                          | 앱 로그를 봅니다 — `%LOCALAPPDATA%\Claude-3p\Logs\main.log`                                   |


### EDR·백신에 막히는 경우

회사가 CrowdStrike Falcon·Microsoft Defender ASR·AppLocker 같은 보안 소프트웨어를 쓰면, Cowork 가 내부적으로 실행하는 프로그램이 차단될 수 있습니다. **앱은 정상으로 보이고 설정도 읽히는데 Cowork 세션만 시작되지 않는** 형태라 방화벽 문제와 구분이 안 됩니다.

차단되는 대상은 이 파일입니다.

```
%LOCALAPPDATA%\Claude-3p\claude-code\<버전>\claude.exe
```

⚠️ **경로가 아니라 서명자로 허용하십시오.** 게시자는 `Anthropic, PBC` 입니다. 경로에는 버전 번호가 들어 있어서, 경로로 예외를 걸면 앱이 업데이트될 때마다 다시 막힙니다.

> 📖 [Endpoint security software](https://claude.com/docs/third-party/claude-desktop/installation#endpoint-security-software) — macOS(Santa Team ID `Q6L2SF6YDW`) 쪽 값도 여기 있습니다.


---



## 6. 조직 배포

테스트가 끝나면 같은 레지스트리 값을 MDM(GPO·Intune)으로 밀어넣습니다 — 내용은 동일하고 전달 수단만 바뀝니다. **수십 대 이상이면 손으로 쓰지 말고** 앱 안에서 설정을 만들어 배포 파일로 내보냅니다 — 오타·형식 사고가 사라집니다.

1. **관리형 설정 없는 PC**에서 앱 실행 → 로그인하지 않은 채 `Help → Troubleshooting → Enable Developer Mode` → `Developer → Configure Third-Party Inference…`
2. 좌측 항목을 위에서부터 채움 — `Connection`(게이트웨이 주소·모델 목록·helper 경로) → `Workspace restrictions` → `Telemetry & updates`
3. `Apply locally`로 그 PC에서 먼저 확인 → 되면 `Export`
4. 나온 파일을 MDM으로 배포 후 앱 배포 — **설정이 먼저**

⚠️ 관리형 설정이 이미 깔린 PC에서는 이 창이 읽기 전용 — 설정 안 된 PC에서 작업합니다.


| 내보내기 형식             | 어디에 쓰나                                  |
| ------------------- | --------------------------------------- |
| `.reg`              | 그룹 정책(GPO) 가져오기, Intune 스크립트            |
| `.zip` (ADMX 템플릿)   | Intune·GPO 관리 콘솔에서 값을 직접 입력할 때 (틀만 나옵니다) |
| `.mobileconfig`     | macOS — Jamf·Intune 등 Apple MDM         |
| `.plist` (Profile Manifest) | macOS — Jamf·ProfileCreator 용 틀     |


이 창에는 **Egress Requirements** 항목이 있어, 지금 만든 설정에 필요한 **방화벽 허용 호스트 목록을 그대로 만들어 줍니다** — `.txt` 로 내보내 네트워크 담당자에게 넘기면 되고, **Test connectivity** 로 그 자리에서 시험도 됩니다. §2 「망 쪽」 표를 손으로 옮겨 적는 것보다 이쪽이 정확합니다.

⚠️ **Windows 에서 부서마다 설정을 다르게 하려면 주의가 필요합니다.** MDM 이 사용자 단위로 배포하면 값이 `HKCU` 에 들어가는데, `HKLM` 에 값이 하나라도 있으면 `HKCU` 는 통째로 무시됩니다(절차 4). 그룹별로 나누려면 **전부 `HKCU` 로 배포하고 `HKLM\SOFTWARE\Policies\Claude` 를 비워 두십시오.**

> 📖 절차 원문은 [Deploy with MDM](https://claude.com/docs/third-party/claude-desktop/mdm), Intune·SCCM·GPO 로 MSIX 를 미는 방법은 [Windows 배포 가이드](https://support.claude.com/en/articles/12622703-deploy-claude-desktop-for-windows). 나머지 문서는 **§9 참고**에 모아 뒀습니다.


---



## 7. 선택 토글 — 앱 기능 켜고 끄기

게이트웨이 연결과 무관한 앱 기능입니다 — 안 해도 됩니다. 모두 절차 4와 같은 `HKLM\SOFTWARE\Policies\Claude`에 넣고, **앱을 완전히 종료 후 재시작**해야 반영됩니다.

**공통 절차** (모든 토글 동일) — ▶ 🔴:

```powershell
$K = "HKLM:\SOFTWARE\Policies\Claude"
Set-ItemProperty $K <키이름> "true"          # 값 넣기
(Get-ItemProperty $K).<키이름>              # true 확인
Get-Process -Name "*laude*" -ErrorAction SilentlyContinue | Stop-Process -Force   # 앱 완전 종료 (백그라운드 잔류 시 미반영)
```

재시작 후 확인. 안 바뀌면 ① 값이 찍히는지, ② `"1"`로도 시도. 되돌릴 땐 `false`가 아니라 **키 삭제** — `Remove-ItemProperty $K <키이름>` (⚠️ 절차 4의 `inference*` 6개는 지우면 연결이 끊깁니다).

**켤 때 넣는 키** (기본 off — 미설정이면 옵션 자체가 안 보입니다):

| 키 | 넣으면 |
|---|---|
| `chatTabEnabled` | Chat 탭 표시 |
| `autoModeEnabled` | "Act without asking" 승인 옵션 (모델이 Claude 4.6+여야 활성 — `claude-haiku-4-5-20251001`은 미달) |
| `disabledBuiltinTools` | 적은 내장 도구를 제거 — JSON `'["WebSearch","WebFetch"]'` |
| `builtinToolPolicy` | 도구별 승인 묻기 — JSON `'{"Bash":"ask"}'` |
| `managedMcpServers` | 조직 지정 MCP 서버 노출 |
| `disableBundledSkills` | 내장 Skills 끄기 |
| `allowedWorkspaceFolders` | Cowork 폴더 제한 — `'[{"path":"C:\\Work"}]'` (객체 목록, 빈 `[]`이면 전부 차단) |
| `isDesktopExtensionEnabled` | 직원이 확장 설치 가능 |
| `disableAutoUpdates` | 자동 업데이트 끄기 (폐쇄망은 필수 — 새 버전은 IT가 offline판으로 배포) |
| `autoUpdaterEnforcementHours` | 강제 재시작까지 시간 단축(1~72, 기본 72) |

**끌 때 넣는 키** (기본 on — `"false"`를 넣음):

| 키 | `"false"`면 |
|---|---|
| `coworkTabEnabled` | **Cowork 탭 제거 — 이 문서의 목적 자체가 사라짐.** Cowork가 안 보이면 이 값을 회사 정책이 꺼둔 것일 수 있음 (구 지원 문서의 `secureVmFeaturesEnabled`는 구식 명칭 — 쓰지 말 것) |
| `isClaudeCodeForDesktopEnabled` | Code 탭 제거 |
| `isLocalDevMcpEnabled` | 직원 자체 MCP 서버 금지 |

주의할 값 형식:

- `disabledBuiltinTools`/`builtinToolPolicy`/`managedMcpServers`/`allowedWorkspaceFolders`/`inferenceModels`는 **JSON을 통째로 한 문자열**로 (`Set-ItemProperty $K x '["a"]'`). 도구 이름은 앱 표기 그대로 — 틀리면 오류 없이 무시됩니다.
- `allowedWorkspaceFolders`는 `path`를 가진 객체 목록 + JSON 이스케이프(`C:\Work` → `C:\\Work`). **Claude가 지정 폴더 밖을 읽는 것만 막고, 직원이 📎로 파일을 올리는 건 못 막습니다** — 그런 키가 없습니다.
- `disableAutoUpdates`+`autoUpdaterEnforcementHours`는 예외 취급 — 둘만 넣으면 나머지 설정은 관리형이 아니라 직원 설정이 남습니다. 다른 키를 같이 넣는 순간 전체가 관리형이 됩니다.
- `disabledBuiltinTools`는 **앱 내장 도구** 얘기 — 게이트웨이 서버측 웹서치(`routing_profiles.web_search_enabled`, /apps 토글)와 무관합니다.

지원 문서에만 있는 키(레퍼런스에 없음): `effortLevel`, `forceLoginOrgUUID` — 쓰기 전 [Enterprise configuration](https://support.claude.com/en/articles/12622667-enterprise-configuration)에서 확인. 전체 키는 [Configuration reference](https://claude.com/docs/third-party/claude-desktop/configuration)가 정본입니다.



## 8. 검증 기록

**2026-08-03 · Windows Server 2025 (EC2 `m5zn.metal`) · Cowork 1.24012.9.0**

절차 1~5 를 순서대로 실행해 앱 응답과 서버측 웹서치까지 확인했습니다. 게이트웨이 쪽 확인은 `./deploy doctor` 의 usage_logs 조회로 했습니다.


| 확인한 것        | 결과                                                                 |
| ------------- | ------------------------------------------------------------------ |
| `client` 분류   | 5행 모두 `cowork` — 앱이 보내는 `anthropic-client-platform: desktop_app` 을 게이트웨이가 읽습니다 |
| 모델            | `claude-opus-5` 와 `claude-haiku-4-5-20251001` 이 함께 기록 — 앱이 보조 작업에 작은 모델을 씁니다 |
| 서버측 웹서치       | 실시간 질문 행에 `web_search_count = 1`                                    |
| 비용 기록         | 전 행 `cost_usd > 0`, `status = SUCCESS`                              |
| CloudFront 경로 | `onboard-windows.ps1` 의 `gateway health: 200`                       |


⚠️ **비용 검산 시 캐시 토큰을 빼먹지 마십시오.** `input_tokens`·`output_tokens` 만으로 계산하면 실제 `cost_usd` 보다 훨씬 작게 나옵니다. Cowork 는 큰 시스템 프롬프트를 캐시로 보내서, `cache_creation_tokens`·`cache_read_tokens` 가 비용의 대부분을 차지하는 행이 나옵니다. 단가 오류로 오해하기 쉽습니다.

**이 검증에서 고친 것들** — 실제로 막혔던 지점입니다.

- ⓪ 표 한 칸에 붙어 있던 `winget` 명령 두 개가 한 줄로 읽혔습니다
- ⓪ 에 새 창 확인 단계가 없어, `py` 는 되는데 `git` 만 안 되는 상태로 넘어갔습니다
- 절차 3 에 설치 파일 받는 곳이 없었습니다
- 절차 4 의 자리표시자가 블록 한가운데 있어 그대로 레지스트리에 들어갔습니다
- 절차 2 에 helper 파일을 만드는 명령이 없고 내용만 있었습니다



---



## 9. 참고 — Anthropic 공식 문서

이 문서의 근거입니다. **값이 어긋나면 아래가 정본**입니다 — 벤더가 갱신하므로, 여기 적힌 주소·기본값이 안 맞으면 원문을 먼저 보십시오.

**기술 문서** (개발자·배포 담당)


| 문서                                                                                             | 여기서 인용한 부분                                        |
| ---------------------------------------------------------------------------------------------- | ------------------------------------------------- |
| [Installation and setup](https://claude.com/docs/third-party/claude-desktop/installation)       | 기기 요구사항·점검 도구(절차 0), offline 설치판(절차 3), EDR 예외(§5), 자동 업데이트 동작(§7) |
| [Configuration reference](https://claude.com/docs/third-party/claude-desktop/configuration)     | 설정 키 전체 — 절차 4 의 `inference*` 6개와 §7 토글의 원문 정의     |
| [Deploy with MDM](https://claude.com/docs/third-party/claude-desktop/mdm)                       | 레지스트리 위치·우선순위·형식(절차 4), 조직 배포 절차와 내보내기 형식(§6)      |
| [Telemetry and egress](https://claude.com/docs/third-party/claude-desktop/telemetry#required-egress-paths) | 방화벽 허용 호스트(§2 「망 쪽」)                     |
| [Bootstrap server](https://claude.com/docs/third-party/claude-desktop/bootstrap)                | §3 에서 **쓰지 않기로** 한 방식 — 판단 근거                      |


**지원 문서** (IT 관리자)


| 문서                                                                                                     | 내용                              |
| ------------------------------------------------------------------------------------------------------ | ------------------------------- |
| [Windows 배포](https://support.claude.com/en/articles/12622703-deploy-claude-desktop-for-windows)          | Intune·SCCM·GPO 로 MSIX 대량 배포    |
| [macOS 배포](https://support.claude.com/en/articles/12611117-deploy-claude-desktop-for-macos)              | MDM 배포 (이 문서는 Windows 전용)       |
| [Enterprise configuration](https://support.claude.com/en/articles/12622667-enterprise-configuration)     | 조직 정책 키 요약 — §7 토글의 관리자용 대응표    |


**설치 파일 주소** — 사람이 보는 다운로드 페이지가 아니라 **항상 최신판으로 연결되는 고정 주소**라, 배포 자동화에 그대로 씁니다.


| 용도                | 주소                                                                  |
| ----------------- | ------------------------------------------------------------------- |
| 점검 도구 (x64)       | `https://claude.ai/api/desktop/win32/x64/cowork-readiness-check/latest/redirect`   |
| 점검 도구 (arm64)     | `https://claude.ai/api/desktop/win32/arm64/cowork-readiness-check/latest/redirect` |
| offline 설치판 (x64)   | `https://claude.ai/api/desktop/win32/x64/offline/latest/redirect`   |
| offline 설치판 (arm64) | `https://claude.ai/api/desktop/win32/arm64/offline/latest/redirect` |


⚠️ **`https://claude.com/download` 는 쓰지 마십시오.** 그 페이지는 Windows 에 `.exe` 를 주는데, **그것으로 깔면 Cowork 가 빠집니다**(절차 3). 개인 사용자용 안내 페이지라 조직 배포 경로가 아닙니다.

ℹ️ `downloads.claude.ai` 와 `releases.claude.com` 은 **다운로드 안내 주소가 아닙니다** — 각각 설치 파일이 실제로 전송되는 CDN 과 업데이트 확인 전용 호스트입니다. 직원에게 알려줄 주소가 아니라 **방화벽에 넣을 이름**입니다(§2).

