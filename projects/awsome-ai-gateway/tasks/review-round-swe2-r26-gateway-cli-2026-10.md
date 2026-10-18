# R26 — gateway-cli (v1) + gateway-cli-v2 (installer/entrypoints) 리뷰

**라운드 방식**: SWE-2 직접 리뷰(kiro 불가 — 토큰 소진). 코드 정독 + 실측 + 회귀 테스트 + 독립 자체 재검토.
**범위**: `gateway-cli/` (v1 wheel), `installer/packaging/entrypoints/gateway-cli-v2/` (v2 설치 경로 전체 — api-key-helper, statusline, oidc 클라이언트, cli 서브커맨드군).
**제외**: admin-chat-agent(NDS 미배포), cowork-installer(R27), db/ 마이그레이션(R28).

## 검증 결과

| 스위트 | baseline | 최종 |
|---|---|---|
| gateway-cli (v1) pytest | 156 | **160 passed** |
| cli-v2 pytest | 173 | **194 passed** |
| ruff (cli-v2) | — | clean |
| py_compile (전수) | — | clean |

## 처리한 발견 (S급 — 실배포 영향)

- **VK 만료 naive-datetime 계열 (3곳 + 2곳 재발견)**: 서버가 offset 없는 `expires_at`을 주거나 생략 시 naive datetime 생성 → api-key-helper daemon이 `naive - aware` TypeError로 사망 / `login._exchange_jwt_for_vk`의 `.timestamp()`는 로컬시간 해석 → KST 호스트에선 VK가 실제 만료 후 ~9h까지 fresh로 취급. `vk_client.py`(v1+v2), `oidc_client.py`(v1+v2), `login.py`, `sso.py`(v1+v2) 전부 UTC-aware 정규화.
- **자격증명 캐시 TOCTOU**: `write_text()` 후 `chmod(0600)` 사이 창에 토큰/VK 파일이 umask 권한으로 노출 → `os.open(O_CREAT, 0o600)`으로 처음부터 0600 (`oidc_client._secure_write`, `login._secure_write`; 기존 파일엔 chmod 재주장).
- **설정 파일 비원자적 쓰기**: `settings.json`/`managed-settings.json`의 truncate+write는 중간 크래시 시 복구 불가 손상 → `cli/utils/files.py`의 temp+`os.replace` 원자 쓰기로 전환 (v2 user/managed/teardown, v1 managed Unix의 sudo tee → sibling temp + `sudo mv`).
- **`remove_gateway_settings`가 org 사전값 유실**: disable 시 우리가 덮어쓴 키를 그냥 pop → earliest managed 스냅샷에서 org 값 복원 (`earliest_backup` 공용 헬퍼 — teardown과 동일 규칙).
- **teardown이 reconcile-제거 키 복원 누락**: setup이 제거한 BYPASS 키(`ANTHROPIC_API_KEY`, `CLAUDE_CODE_USE_BEDROCK`, `modelOverrides`, `statusLine` 등)는 owned키가 아니라 미복원 → earliest 스냅샷에서 부재 키만 복원(사용자 재추가 키는 건드리지 않음).

## 처리한 발견 (N급)

- **부분 OIDC 설정 시 STS 무음 폴백**: `OIDC_ISSUER_URL`만 있으면 조용히 IAM-ARN 기반 VK 발급(= 다른 신원) → v2 `_detect_mode`에 v1과 동일한 경고 추가.
- **`env --persist` 셸 인젝션/손상**: 값의 `\`, `"`, `$`, 백틱 미이스케이프 → `_escape_posix_value`; PowerShell 스니펫도 `` ` ``/`"`/`$` 이스케이프. 스킵 판정 정규식을 라인 앵커로 강화(주석의 `VAR=` 언급이 persist를 억제하지 않게).
- **`_strip_posix_rc` 범위 과다**: 마커와 무관하게 우리 변수 export 전부 삭제 → 마커 직후의 연속 블록만 제거(사용자 직접 작성 export 보존). 이스케이프된 `"`를 정규식이 못 잡던 것도 수정(`(?:[^"\\]|\\.)*`).
- **`verify` 4xx false-green**: `/health` 4xx(잘못된 URL/포트 스왑)를 OK 처리 → WARN (2xx만 OK, 5xx는 기존대로 FAIL).
- **`settings["env"]` 비-dict 크래시**: 손상된 설정의 `env`가 문자열/리스트면 managed 쓰기 **이후** put_user가 TypeError → setup 초반에 정규화.
- **`setup`/`onboard` 모델 게이트 드리프트**: onboard이 roster 없을 때 FALLBACK 멤버십 강제 + `--model` 형식 미검증 + roster alias 미검증 → 공통 `_gate_model` (alias 검증 + 명시 roster 시에만 멤버십, roster alias도 각각 검증).
- **v1 `statusLine` bare 이름**: PATH 없는 박스에서 조용히 무효 → PATH 해석 → helper sibling 폴백; `apiKeyHelper`/`statusLine` 공백 경로 셸 분리 방지 인용.

## 자체 재검토에서 추가로 잡은 것

- `login._exchange_jwt_for_vk` naive `.timestamp()` (위 S급 계열 — 첫 패스에선 vk_client만 봤음)
- `sso.py` AWS 자격증명 만료 naive 시 동일 크래시 가능 → 방어 정규화
- v1 `apiKeyHelper` 공백 경로 미인용 (테스트 추가)
- ruff --fix 잔여: 범위 밖 3파일 import 정리는 dead-import라 안전 확인 후 유지, `statusline/main.py` F841(`log` 미사용) 정리

## 확인됨(수정 불요)

- Windows uninstall: ARP 엔트리 product-identity 검증, suspicious path 거부, `ShellExecuteW(runas)` 구조화 호출, SYSTEM 감지 — 이미 경화됨
- PKCE/S256 + state 검증 + 127.0.0.1 콜백, Cognito 강제 비밀번호 리셋, VK 갱신 daemon 루프
- `api-key-helper` stdout 계약(VK만 stdout, 로그는 stderr) 유지

## 잔여/후속

- `openai_config.setup_jwt` — 테스트만 있고 호출부 없음 (dead/unwired). R29 정리 후보.
- Windows/macOS 네이티브 경로(Inno uninstaller, `C:\Program Files\ClaudeCode`, `sudo chown`)는 Linux 환경에서 실행 검증 불가 — 코드 리뷰 수준.
- R25 유보(N-UI6 팝업, 서비스토큰 synthetic user, stale admin_role 쿠키) — NDS는 admin-chat 미배포라 저우선순위.
