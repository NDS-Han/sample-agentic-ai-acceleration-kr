# R32 — cowork-installer 리뷰

**라운드 방식**: SWE-2 직접 리뷰. R27에서 스킵됐던 최종 대형 영역. 코드 정독 + installer/(R26 대조 diff) + 회귀 테스트.
**범위**: `cowork-installer/` 전체 — `installer/packaging/` (build.ps1, installer.iss, gateway_cli.spec, download_wheels.ps1, site-config/site-extra 예시) + `entrypoints/gateway-cli-v2/` (cli 전 커맨드, api-key-helper, gateway_cli_oidc).
**배경**: 이 트리는 `installer/…/gateway-cli-v2`의 **Cowork(Claude Desktop) 변환 포크** — settings.json 대신 OS 네이티브 managed config(Windows `HKCU/HKLM\SOFTWARE\Policies\Claude`, macOS configLibrary)를 쓰고, api-key-helper가 `inferenceCredentialHelper`로 동작.

## 검증 결과

| 스위트 | baseline | 최종 |
|---|---|---|
| cli-v2(cowork) pytest | 83 | **90 passed, 6 skipped** |
| installer 트리 대조 diff | — | 의도적 변환만 잔존 |

## 처리한 발견 (S급 — R26 수정 미백포트 재발)

포크 시점이 R26 수정 이전이라 R26이 고친 결함 세 그룹이 그대로 살아있었음:

- **VK 만료 naive-datetime (4곳)**: 서버가 offset 없는 `expires_at`을 주면 naive datetime 생성 →
  - `oidc_client.exchange_jwt_for_vk`: `_run_oidc_daemon`이 `naive - aware` TypeError로 사망 / `.timestamp()`가 로컬시간 해석 → KST 호스트에선 VK가 실제 만료 후 ~9h까지 fresh 취급
  - `cli.login._exchange_jwt_for_vk`: 동일 `.timestamp()` skew
  - `api_key_helper/vk_client.py` (STS 경로): 동일 + fallback이 `datetime.now()` (naive local)
  - `api_key_helper/sso.py`: AWS 자격증명 만료 파싱 동일 크래시 가능
  → 전부 `tzinfo is None → replace(tzinfo=utc)` 정규화 + fallback을 `datetime.now(utc)`로 (R26과 동일 패턴)
- **자격증명 캐시 TOCTOU (2곳)**: `oidc_client`(tokens+VK)와 `cli.login._secure_write`가 write_text+chmod — 창 사이 umask 권한 노출 → `os.open(O_CREAT, 0o600)` atomic-0600 백포트
- **부분 OIDC env 무음 STS 폴백**: `_detect_mode`가 `OIDC_ISSUER_URL`만 있어도 조용히 STS — IAM-ARN 기준 *다른 신원*으로 VK 발급 → R26 경고 백포트 (dev 빌드에서만 발생 가능 — `apply_baked_env_fallback`이 baked 빌드에선 둘 다 채움)

**회귀 테스트**: `tests/test_r32_backports.py` 신규 — 0600-atomic 3건, naive→UTC/aware 4건 (R26 테스트 케이스 이식 + vk_client/sso 추가).

## 확인됨 (이미 경화 — 수정 불요)

- **uninstaller 검증 (cowork_uninstall.py)**: ARP UninstallString을 GUID substring → `unins###.exe` 파일명 → install-dir leaf 일치(`gateway-cli-cowork`) → DisplayName 대조 4중 검증 후 ShellExecuteW(`runas`, file/params 분리 — shell reparse 불가). HKCU-writable ARP hive의 planted-entry 공격 차단. `AppId={{GUID}}` 이중 닫힘 중괄호(`{GUID}}_is1`)도 substring 매칭으로 흡수.
- **CA fingerprint pin (cowork_ca.py)**: build-time SHA-256 pin, 불일치 시 설치 거부 + `--force` 탈출구(합법 CA rotation). non-critical BasicConstraints 강제 안 함(실제 사내 CA가 그 형태라 pin이 정답). env-override PEM은 pin 없을 때 `--force` 요구.
- **machine-scope(HKLM) 방어 (managed.py)**: HKLM+static credential 거부(전유저 읽기 가능 → VK 유출), machine scope에서 user-profile 하 helper 거부(타 유저 실행 불가), `%ProgramData%` state dir을 SYSTEM/Admins-only ACL로 경화(R2-1: unprivileged 사용자가 elevated rollback state를 심는 것 차단), machine setup flock + stale-steal, `_release_locked_env`로 frozen 빌드에서 machine-path env override 차단.
- **UAC auto-elevation (elevation.py)**: TOKEN_ELEVATION_TYPE=LIMITED(동일-신원 consent)일 때만 자동 승격 — standard user의 credential prompt(다른 admin → 잘못된 HKCU hive)는 의도적으로 제외. ShellExecuteExW로 exit code 전파.
- **HKLM precedence trap**: `hklm_conflict`/`machine_scope_active`가 "우리 것 vs 외래 HKLM" 구분 (fingerprint 마커), verify에서 FAIL 처리.
- **Rollback saga (utils/rollback.py)**: arm-before-mutation, checked-results(not-ok = 실패), best-effort unwind + 원본 예외 우선. setup이 CA→config 2단계를 saga로 묶음.
- **MSIX AUMID relaunch**: `shell:AppsFolder\<PFN>!<AppId>`로 시작(버전 경로 직접 실행 금지), SYSTEM/session-0 및 root에서 GUI 구동 거부 + 수동 힌트.
- **stdout 계약 (api-key-helper)**: VK 한 줄만 stdout, 로그는 전부 stderr — `inferenceCredentialHelper`/`apiKeyHelper` 호출자 계약 유지. structlog sink를 TLS inject 전에 설치.
- **CP949 대응**: entry shim + main.py에서 PYTHONUTF8/stdout·stderr UTF-8 강제, `_run_powershell`이 양쪽 UTF-8 고정(한국어 Windows mojibake/crash 방지).
- **truststore/botocore RecursionError**: `without_os_trust_store()` 컨텍스트로 boto3 호출 시 패치 해제 — 이미 문서화된 우회.
- **PKCE 플로우 (login.py/oidc_client.py)**: S256 + state 검증 + 127.0.0.1 바인드 + 인메모리 이벤트 조율(브라우저에 진짜 결과 표시), refresh token 회전, Cognito InitiateAuth 헤드리스 폴백(NEW_PASSWORD_REQUIRED 처리).
- **installer.iss**: `PrivilegesRequired=admin`, 고정 `C:\Gateway-CLI-Cowork`(machine-readable 필수), HKCU-aliasing 경고 코멘트, scope 선택은 파일로만 기록(`registry-scope.conf` → setup이 읽음 — elevated install이 HKCU에 쓰면 admin hive에 가는 함정 회피), PATH는 HKLM+HKCU 양쪽, Inno uninstaller 위임(CLI는 자기 이미지 삭제 불가).
- **baked env fallback (config.py)**: Start-menu MSIX 실행이 `HKCU\Environment`를 상속 안 해 OIDC env가 없는 문제 → baked site_defaults로 보완(명시 env가 항상 우선).

## 소소한 관찰 (수정 안 함)

- `cli/models.py` docstring의 "anything else is rejected"는 과장 — roster 없는 `--model`은 installer 측 `_gate_model`과 동일하게 무게이트 통과(사이트 커스텀 alias 재빌드 불요의 의도된 의미론). 문구만 부정확.
- `data_dir()`의 `GATEWAY_CLI_DATA_DIR`는 frozen 빌드에서도 유효 — per-user 토큰 스토어 재지정이라 위험 낮음(machine_* 만 release-locked, 일관된 설계).
- `cowork_ca._run_powershell`이 PEM 경로를 PS 스크립트에 직접 보간 — 값은 사용자 본인 env(`GATEWAY_CLI_CA_BUNDLE`)/baked라 실질 위험 낮음.
- `installer.iss`의 per-user install 언급 코멘트는 `PrivilegesRequired=admin`에서 도달 불가한 잔재(무해).

## 잔여/후속

- R26 이후 installer 측 수정 중 이 포크에 해당 없는 것만 남은 것 확인(settings.json/reconcile/env-persist/statusline/teardown 등은 Claude Code 전용). 다만 **포크 드리프트 자체가 반복 재발 패턴** — R26 수정이 이번에 다시 필요했던 것처럼, 향후 installer 수정 시 cowork 트리 동기 확인 절차가 없으면 같은 회귀가 또 생길 수 있음. 공유 코드를 모듈로 뽑거나 최소한 diff 체크리스트가 필요.
- Windows 전용 경로(레지스트리 쓰기, icacls, UAC, Inno unins000)는 Linux 환경에서 실행 검증 불가 — 코드 리뷰 + 단위 테스트(레지스트리 모킹) 수준.
- `--model`이 baked roster 전체를 대체([X] 단독)하는 의미론도 installer와 동일 — 의도적이지만 roster 유지가 나을 수도 있음(빌드된 COWORK_MODELS가 있어도 `--model` 하나만 쓰면 나머지가 사라짐).
