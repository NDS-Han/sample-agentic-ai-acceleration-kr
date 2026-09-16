클라이언트식별 가능해야함
- 클로드코드 사용을 위해 게이트웨이에 접근하는것
- 코워크 사용을 위해 게이트웨이에 접근하는 것 


베드락 호출 계정 분리
- 클로드코드로 들어올때의 베드락 호출 어카운트와 코워크로 들어올때의 베드락 호출하는 어카운트가 달라야함 (멀티어카운트 지원 및 어카운트 변경 간단히 가능해야함)


베드락 호출 방식
- 클로드코드로 들어와서 호출할때는 runtime 혹은 mantle 지원
- 코워크 들어와서 호출할때는 mantle 서빙 모델만 지원(Opus4.8 mantle 서빙모델) 일단은 도쿄리전


사용자(User) 풀 분리
- Claude Code 사용자와 Cowork 사용자는 별도 풀로 관리 (각각 인증하므로). 한 사람이 양쪽을 쓰면 각 앱에서 별도 인증.
- 구현: 사용자 풀 분리를 org/team 경계로 실현 → 기존 scope(TEAM/ORG) 메커니즘이 앱 분리 역할도 겸함.

거버넌스(앱별)
- 앱별 사용량/예산/rate-limit/모델 접근제어/차단.
- 신뢰축 = VK(인증) + team/org(소속). 식별축 = User-Agent surface 토큰(참고·로깅용).

통합 대시보드
- 하나의 대시보드에서 Claude Code와 Cowork를 동시 모니터링 (앱별 분리 집계 + 통합 뷰).

---

## 확정 설계 요약 (2026-06-19, 브레인스토밍 결과)

### 클라이언트 식별 (실측 확정)
- **Claude Code**: UA `claude-cli/X (external, cli|sdk-cli)`
- **Cowork**: UA `claude-cli/X (external, claude-desktop-3p|local-agent)` + 일부 `anthropic-client-platform: desktop_app`
- ⚠️ `x-app: cli`·`claude-code-20250219`·`X-Stainless-*` 는 **양쪽 공통** → 단독 식별 금지. UA surface 토큰으로 가르고 Cowork 먼저 체크.

### 라우팅 (접근법 A — 데이터 기반 routing_profiles + Mantle 어댑터)
| client | 백엔드 | 계정 | 리전 | 모델 |
|--------|--------|------|------|------|
| claude-code | InvokeModel(runtime) [기본] / Mantle(옵션) | 374 (자기 계정 IRSA) | ap-northeast-2 | 기존 alias |
| cowork | **Mantle 전용** | **905** (cross-account role assume) | **ap-northeast-1 (Tokyo)** | **Opus 4.8** (`jp.anthropic.claude-opus-4-8` 또는 Mantle 서빙) |
- 계정 분리 = cross-account AssumeRole(A): 905에 role, 374 게이트웨이가 assume. **키 보관 0**. 계정/방식 변경 = routing_profile row 한 줄.
- Mantle 인증 = `aws_bedrock_token_generator.provide_token(region)` (SigV4→단기 bearer, role 임시자격 호환). 레퍼런스: admin-chat-agent `main.py:201`.

### 검증된 전제 (2026-06-19 라이브)
- 905 키 유효(`user/test-admin`), Tokyo Bedrock에 Opus 4.8 존재(`anthropic.claude-opus-4-8`, profile `jp.anthropic.claude-opus-4-8`), Mantle 엔드포인트 `bedrock-mantle.ap-northeast-1.api.aws/anthropic` 살아있음(401).
