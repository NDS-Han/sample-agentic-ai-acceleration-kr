# 리뷰: 클라이언트·에이전트 컴포넌트 보안 점검

**범위**: `gateway-cli/`(STS·OIDC 로그인, VK 발급/저장), `installer/` +
`cowork-installer/`(managed config·권한 상승·빌드/배포), `admin-chat-agent/`
(AgentCore runtime·tool Lambda·query_db SQL 게이트), `gateway-clients/`·
`loadtest/`·`mock/` 부가 표면.

**판정: 조건부 수렴** — 핵심 경계(STS SSRF, OIDC 검증, SQL 읽기전용, 권한
상승/롤백)는 다층 경화 상태이며, 잔여 결함은 평문 채널과 파일 권한 두 축에
집중된다.

## 핵심 계약

- STS 경로: CLI 가 서명한 `GetCallerIdentity` 프리사인 URL 을 admin-api 가
  정확-호스트 allowlist + Action 고정으로 검증 후 전달 — 계정/role allowlist
  는 빈값=거부(fail-closed).
- OIDC 경로: PKCE(S256)+state+localhost 콜백 → id_token 을
  `/v1/auth/exchange` 로 교환. 서버는 JWKS(RS 계열) 검증+iss/aud 확인,
  클라이언트는 토큰·VK 를 0600 파일로만 저장하고 logout 시 둘 다 삭제.
- Cowork managed: HKLM 정적자격·user-profile helper 경로·스냅샷 위조는
  전부 거부/검증. Claude Code installer 는 managed-tier(0644)+user-tier 양층.
- query_db: 단일 SELECT/WITH 만 — AST 전체 DML/DDL/잠금/위험함수 스캔 +
  traverse_scope 테이블 화이트리스트 + 미커버 Table fail-closed +
  EXPLAIN 비용 상한 + 강제 LIMIT + 10s timeout. DB 롤은 컬럼 단위
  SELECT-only (`gateway_chat_reader`).
- AgentCore: admin-api 가 `require_admin`+세션 `user_id` 소유 검증 후
  SigV4 `invoke_agent_runtime`; Code Interpreter 는 SANDBOX(네트워크 없음).

## 발견·수정 후보 결함

### MEDIUM

- **OTEL bearer 토큰이 world-readable managed settings 에 기록**:
  `installer/.../cli/managed.py:457-460` 의
  `OTEL_EXPORTER_OTLP_HEADERS=Authorization=Bearer <token>` 이
  `sudo chmod 644`(`:897`)로 쓰이는
  `/etc/claude-code/managed-settings.json` 과
  `managed-settings.d/50-gateway.json`(`:621,649`) 양쪽에 실린다 —
  로컬 임의 사용자가 공유 수집 자격을 읽어 임의 user.id 로 위조
  텔레메트리 주입·오프박스 재생 가능. cowork 트리는 같은 이유로
  머신 범위 정적자격을 거부하는데 정책이 불일치.
- **자격 전달 URL 에 스킴 강제 없음**: issuer/admin-api/gateway URL 이
  env·YAML·CLI·baked site-config 어디서도 https 검증 없이 사용된다
  (`gateway-cli/src/gateway_cli_oidc/oidc_client.py:64-71,82-90` +
  `exchange_jwt_for_vk`; `api_key_helper/{config,vk_client}.py`;
  cowork v2 동일본). `build.ps1:264-314` 도 무검증 베이크, cowork
  managed.py:367 의 https 체크는 `inferenceGatewayBaseUrl` 뿐.
  문서상 내부 ALB `http://` 형태가 기본값
  (`container_login_gw.md:31,61`)이라 id_token·VK·프리사인 URL 이
  평문으로 흐른다.

### LOW

- **OTEL 엔드포인트 자동 유도가 `http://host:80` 고정**
  (`installer/.../cli/main.py:196`) — bearer 헤더+프롬프트 로그가 평문.
- **토큰/VK 캐시 비원자적 쓰기** — `O_TRUNC` 인플레이스, tmp+rename·fsync
  없음(`cli/login.py:112-131`, 두 트리 동일) — 크래시 시 잘린 파일→
  재로그인 강제.
- **`create_code_interpreter.py` dev 롤 ARN 기본값**
  (`arn:aws:iam::123456789012:...-dev-...`) — prod 에서 무수정 실행 시
  외부계정 role 로 인터프리터 생성 → 세션 시작 실패.

## 검증 완료 — 결함 아님(경화 확인)

- STS SSRF: 정확-호스트 allowlist·Action 고정·리다이렉트 미추적·defusedxml·fail-closed allowlist·IAM-user ARN 거부
- OIDC exchange: 실JWKS 검증(RS 계열)·iss/aud·`email_verified`·10 RPM/IP
- 로컬 토큰 저장: `os.open(0o600)`+재chmod·logout 양쪽 삭제·로그 마스킹
- Cowork managed: HKLM 정적자격 거부·스냅샷 소유/경로 검증·ProgramData ACL·hash-pinned wheel·오프라인 payload
- query_db: 파서+DB 롤 이중 게이트 — multi-statement·writable CTE·`SELECT INTO`·`FOR UPDATE`·확장함수·미커버테이블 우회 전부 차단
- 채팅 세션: 매 라우트 `user_id` 소유 검증 + `require_admin` + SigV4
- `seed_dev_data`: import 시점 `ENVIRONMENT != "dev"` 거부 + 별도 쓰기 시크릿
- `db/init/*.sql`: NOLOGIN·컬럼스코프 SELECT·`chat_agent` 스키마 revoke·statement_timeout
- `loadtest/`·`mock/`·`gateway-clients/`: env 제공 키·플레이스홀더만, 선적 시크릿 없음

## 잔여 리스크

- query_db 함수 denylist 는 패턴 기반 — 신규 PG 함수/확장은 추가 필요.
  실질 최종 방어선은 `gateway_chat_reader` grant.
- staging 버킷은 runtime role 전체 RW — 세션 prefix 격리가 앱 계층.
- `/v1/auth/exchange` 레이트리밋 Redis 장애 시 fail-open — 유효 JWT 없이는
  VK 불가라 폭발반경 작음.
- unsigned installer 는 경고만 — 배포 정책으로 서명 필수화 필요.
- `--api-key` argv 노출은 수용 리스크.
- 라이브 E2E(실행기·OIDC·설치→clear 왕복)는 미검증.
