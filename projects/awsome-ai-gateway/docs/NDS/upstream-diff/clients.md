# 클라이언트·인스톨러·정책 도구 — upstream 대비 변경 전체

upstream은 사용자가 env를 수작업으로 붙여넣는 방식이었다. NDS는
**배포된 게이트웨이에서 직원 env를 뽑아 인스톨러로 설치**하는 경로를
신설했고, CLI·정책 헬퍼·OTEL 배선을 추가했다.

## 1. 직원용 인스톨러 (`installer/`) — 신규

| 구성 | 내용 |
|---|---|
| `packaging/` | Windows Inno Setup(`installer.iss`), `build.ps1`, `download_wheels.ps1`(오프라인 휠), entrypoints, `site-config.json.example`·`site-extra.json.example` |
| `packaging-linux/` | deb 패키지 + entrypoints |
| `packaging/tests/` | 202 tests — 관리 설정·OTEL·보안 쓰기 회귀 |
| managed settings | `managed-settings.json` 병합 + `.d/50-gateway.json` drop-in — **OTEL bearer 헤더는 user-tier에만** 기록(0644 관리 설정에 평문 자격 잔존 방지), stale 키 스크럽 |
| `client-values` | `./deploy client-values` — 직원에게 줄 env 4줄을 게이트웨이에서 추출 |

## 2. cowork-installer/ — 신규

Cowork용 별도 인스톨러 트리(docs + installer) — gateway-cli와
별개의 설치 경로.

## 3. gateway-cli (v1 + v2)

- STS presigned URL 로그인 → VK 발급, VK를 두 위치(env/파일)에서 탐색
- **v1+v2 경화** — VK 만료 TZ·0600 TOCTOU·원자 쓰기(`os.open` 0600+
  tmp+fsync+`os.replace`, tmp명 pid 접미사)·teardown/org 복원·셸 이스케이프
- Mac `build.sh`
- OTEL export 배선 — `OTEL_EXPORTER_OTLP_HEADERS`는 user-tier only,
  `disable`도 user-tier 자격 키 선택 제거(`remove_user_otel_header`)

## 4. policy-helper/ — 신규

Claude Code 정책 조각 도구:

- PoC 1단계: baseline 기본값 출력 → 2단계: **VK로 게이트웨이
  `/v1/policy` 조각을 받아 baseline 위에 병합**
- `baseline.example.json`, `make-baseline-mac.py`,
  macOS 설치 4종(`check/install/make-baseline/rollback-mac.sh`),
  `build.sh`/`build.ps1`, tests

## 5. 운영·스크립트 (deployment/scripts, update-scripts)

사이트 고정이었던 upstream 스크립트를 NDS 명령 아래로 정리하거나 보존:

- `bootstrap-ec2.sh`, `fill-org-values.sh`(org 값 프리셋 + chat-agent
  ARN/서울 placeholder 정리 + REPORTING_TIMEZONE)
- `06-persist-annotations.sh`(어노테이션→values 영구화),
  `09-update-admin-ui.sh`(admin-ui 이미지 빌드·롤아웃),
  `10-switch-https.sh --fresh`(Ingress 전 values https 방식 편집),
  `11-route53-cname.sh`(ALB HTTPS US-06 전환)
- `16-usage-recent.sh`(요청별 토큰·web search·비용),
  `17-set-websearch-caps.sh`(비용 상한 helm 렌더 검증)
- `rebuild-image.sh`(단일 서비스 재빌드/push),
  `set-websearch-url.sh`(AgentCore URL 자동 반영),
  `status.sh`(US-NN 한 줄씩·US-13/17/18 판정)
- `client-vpn.sh`(mutual TLS·split-tunnel Client VPN up/config/status/down)

## 6. 보안 계약 (NDS에서 확정)

- managed 설정은 world-readable — **자격은 user-tier(0600)에만**
- `_secure_write` 원자화 + 모드 0600 + fd 누수·심링크 방어
- `gateway_url`/`admin_api_url`의 http는 내부 ALB 기본값으로 유지,
  **AWS STS fetch만 https 강제**(SSRF 표면)
