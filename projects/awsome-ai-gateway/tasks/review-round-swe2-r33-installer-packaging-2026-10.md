# R33 — installer/ 패키징 레이어 리뷰

날짜: 2026-10 · 대상: `installer/packaging/` + `installer/packaging-linux/` (~1.8k LOC — build/install/spec/.iss/e2e. Python CLI 본체는 R26 커버로 제외)

## 범위

Windows: `build.ps1`(서명·site-config·_site_config 베이크), `installer.iss`(Inno Setup, PATH HKCU/HKLM), `gateway_cli.spec`(PyInstaller), `download_wheels.ps1`, entry shim 3개, `tests/e2e/Invoke-GatewayCliE2E.ps1`.
Linux: `build.sh`, `install.sh`, `make_installer.sh`(.run 자가추출), `deb/build-deb.sh`, `gateway_cli.spec`, `download_wheels.sh`, entry shim 3개, `.gitignore`·README 계약.

## 발견·수정

| # | 심각도 | 파일 | 발견 | 수정 |
|---|---|---|---|---|
| 1 | M | `packaging-linux/build.sh` | **프록시 검증 3키 베이크 완전 부재** — `EXPECTED_PROXY_URL`/`NO_PROXY_VALUE`/`FORBIDDEN_NO_PROXY_TOKEN`을 받는 flag도 env도 site-config 매핑도 없음 → Linux 빌드는 `cli/managed` 상수가 `site_defaults` placeholder로 유지돼 `verify`의 프록시 체크가 조용히 비활성화됨(Windows 빌드만 강제 가능). 사내 프록시 정책 검증이 Windows 전용이 되는 기능 격차 | `--expected-proxy-url`/`--no-proxy-value`/`--forbidden-no-proxy-token` flag + `GATEWAY_CLI_DEFAULT_*` env + site-config 3키 매핑 + `_site_config.py` 베이크 블록에 추가 (build.ps1과 동일 계약). 베이크 블록 단독 실행으로 8키 방출 확인 |
| 2 | M | `packaging/build.ps1` | **site-config.json 로더의 proxy 키 누락** — param/env에는 존재하나 "사이트 관리자가 수정하는 단일 파일" 계약의 JSON 매핑이 5키만 커버 → JSON에 proxy 값을 넣어도 무시됨 | `expectedProxyUrl`/`noProxyValue`/`forbiddenNoProxyToken` 매핑 추가 (param/env > JSON 우선순위 동일 적용) |
| 3 | N | `packaging/`, `packaging-linux/` | **`site-config.json.example` 부재** — 파일이 gitignore로 제외돼 있으나 형식을 알려주는 예시가 없어(동포작 cowork-installer에는 존재) camelCase 키를 스크립트 소스에서 역추적해야 함 | 양쪽에 example 생성 — 8키 + 각 키 의미·우선순위·커밋 금지 주석 (cowork 예시 형식 준용, cowork 전용 키 제외) |
| 4 | N | `packaging/README.md`, `packaging-linux/README.md` | "site-config maps **5 keys only**, proxy는 param/env만" — 수정 후 stale | 8키 + example 경로로 갱신 |

## 검증된 경화 (변경 없음)

- **`build.ps1`**: Authenticode thumbprint/PFX 이중 경로 + RFC 3161 타임스탬프 + sign→verify 게이트; `-Param > env > site-config > site_defaults` 우선순위; `$ErrorActionPreference` 네이티브-stderr 함정(PyInstaller/ISCC/smoke에서 NativeCommandError) 주석·회피 코드; `_site_config.py`를 `ConvertTo-Json` 문자열 리터럴로 생성(인젝션 불가); unsigned 시 명시적 경고.
- **`installer.iss`**: AppId GUID 고정(업그레이드 인식), `PrivilegesRequiredOverridesAllowed=dialog` per-user 설치, PATH 추가/제거가 `IsAdminInstallMode`로 HKLM/HKCU 정확히 분기 + 대소문자 무관 경계 매칭, x64only.
- **`gateway_cli.spec` 양쪽**: `truststore` hidden import(지연 임포트 누락 방지), `site_extra.json` 조건부 datas, pytest/ruff/pip excludes, UPX off(AV/EDR), onedir 공유 `_internal`.
- **`install.sh`**: symlink 런처(PyInstaller `_internal` 해석 보존 — copy가 아닌 link여야 함을 주석 명시), PATH 마커 idempotent, 생성된 uninstaller의 awk가 marker+export 정확히 2행 제거(시뮬레이션 확인), `--prefix`는 항상 `/gateway-cli-suite` 접미사 뒤라 `rm -rf` 안전.
- **`make_installer.sh`**: makeself식 stub+payload, `--extract-only` 모드, `--numeric-owner` tar.
- **`deb/build-deb.sh`**: `--root-owner-group`, `Installed-Size`, postrm은 `-L` 심볼릭링크만 제거(관리자가 재지정한 실파일 보존), `.sha256` 체크섬.
- **`Invoke-GatewayCliE2E.ps1`**: Start-Process + 파일 리다이렉션으로 PowerShell stderr 재포맷팅·NativeCommandError 회피(verify JSON 한 줄 보존), 비밀번호는 cmdline이 아닌 stdin 임시파일로 전달, 401/403=FAIL vs backend 5xx=WARN 분류(크리덴셜+라우팅 소유 범위만 게이트), SYSTEM/SSM 스코프 명시 문서화.
- **`.gitignore` 양쪽**: `site-config.json`, `_site_config.py`, `site_extra.json` 전부 제외 확인(`git check-ignore` 실측) — 환경 식별자 유출 경로 없음.

## 판정 보류(메모)

- `install.sh` PATH 마커: 이전 설치와 **다른 BINDIR**로 재설치하면 마커 존재만 보고 새 export 미기록 — 이전 라인이 옛 bindir 가리킴. 재설치 시 동일 prefix 관례라 edge case.
- E2E의 비밀번호 임시파일은 디스크에 평문 잠시 노출 — cmdline 노출보다 낫고 `finally` 삭제됨. 허용된 trade-off로 문서화됨.
- `.run` stub은 추출 전 payload 무결성 검증 없음 — `.sha256`/GPG `.asc` 배포로 보완되는 구조(사용자 수동 verify).
- installer↔cowork-installer 포크 드리프트는 R32 문서에 잔여 과제로 기록됨 — 이번 proxy 키 격차(Windows=OK, Linux=누락)도 같은 패턴의 변종(플랫폼 간 기능 비대칭)으로 관찰됨.
