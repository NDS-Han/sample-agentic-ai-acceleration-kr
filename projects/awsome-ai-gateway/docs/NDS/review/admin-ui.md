# admin-ui 보안 리뷰 보고서 (최종)

> 상태: 독립 리뷰 완료 + 확정 결함 수정 (unit 27/27 포함 스위트 통과).
> 위협 모델 "서명 검증은 admin-api" 가 일관되게 문서화된 BFF 구조 —
> 신규 HIGH 는 없었고, 잔여는 해당 전제가 깨지는 단독 판정 지점에 집중됐다.

## 개요

Next.js 15 App Router 관리자 콘솔(~40k LOC). NextAuth 미사용 — 자체 OIDC
Code+PKCE 플로우 + dev-login(환경 플래그). 브라우저는 같은 오리진 `/api/*`
프록시와 서버 액션만 호출하고, 실제 인가·서명 검증은 admin-api 가 담당한다.

## 핵심 계약

1. **인가 경계는 admin-api** — middleware/서버 액션은 JWT 서명을 검증하지 않고
   쿠키를 전달만 한다("게이트는 UX, 경계는 API"). UI 게이트 우회(admin_role
   쿠키 조작, role 클레임 위조)는 화면 골격만 노출하고 데이터 호출은 401/403.
2. 쿠키: `admin_jwt`/`admin_role`/`oidc_*` 모두 httpOnly + SameSite=Lax +
   Secure(HTTPS 시). Max-Age 는 토큰 exp 유도(상한 12h).
3. CSRF: 변형 메서드 Origin/Referer 호스트 일치(middleware:139-170) + Lax 이중 방어.
4. 프록시: chat-proxy 는 `/admin/chat/` 세그먼트 화이트리스트 + 정규화 재검증;
   그 외 프록시는 고정 경로 + SAFE_SEGMENT/encodeURIComponent; 바디 상한 필수.
5. 시크릿: VK 는 발급 응답 1회 표시(모달·클립보드), 저장소/URL 미사용.

## 발견·수정된 결함

| # | 위치 | 등급 | 내용 |
|---|------|------|------|
| 1 | `api/client-error/route.ts` | MED | **위조 JWT 통과** — 이 라우트는 admin-api 를 호출하지 않아 "upstream 서명 검증" 전제가 성립하지 않음. 형식+만료만 맞는 위조 쿠키로 무제한 로그 쓰기(8KB/req) 가능 → per-IP fixed-window 레이트리밋(20/min, XFF 우측 홉) 추가 |
| 2 | `lib/auth.ts:172` | LOW | `isSessionExpired` 가 exp 파싱 불가 시 fail-open — dev 토큰은 ISO expires_at 이 파싱되므로 이 분기는 위조/손상 전용 → 해석 불가는 만료로(fail-closed) |
| 3 | `lib/cookies.ts`, `auth/login`, `auth/logout` | LOW | `x-forwarded-proto`/`Host` 신뢰 — 프록시 우회 도달 시 Secure 미부여·redirect_uri 조작(자기 세션/DoS). env 명시 실패 시 derive 대신 거부 권장(잔여) |
| 4 | `ReportCard.tsx` | LOW | `window.open(download_url)` 스킴 미검증 — upstream 조작 시 `javascript:` 실행 → `https:` 화이트리스트(ChartRenderer 와 동일) 추가 |
| 5 | dev-login + middleware 토큰 신뢰 | LOW | `DEV_LOGIN_ENABLED=true` 가 prod 에 도달하면 비인증 ADMIN 발급 — 부팅 경고/prod 가드 권장(잔여) |
| 6 | `budgets/page.tsx` | LOW | `parseJWT` 무방어 — middleware 선검사 의존의 암묵 전제 → try/catch 방어(403 page 와 동일 패턴) |

## 검증 완료 — 결함 아님(경화 확인)

- CSRF: Origin/Referer + Lax 가 서버 액션·logout·dev-login POST 전부 커버
- 오픈 리다이렉트: `callbackUrl`/`next` 파라미터 부재, `//`·`/\` 거부
- 시크릿/URL/localStorage: VK 1회 표시, sessionStorage 는 채팅 세션 ID 만
- XSS: ReactMarkdown raw HTML 미사용·img 스트립·https 전용 이미지·DOMParser sanitize
- SSRF: 전 프록시 화이트리스트·`ADMIN_API_URL` env 전용·경로 순회 이중 검사
- zod 검증이 모든 admin-mutation 입력에 존재; 51개 동적 경로 encodeURIComponent 확인

## 잔여 리스크

- **위협 모델 경계**: "서명 검증은 admin-api" 전제가 깨지는 단독 판정 지점
  (client-error, 페이지 게이트)이 앞으로 늘면 공용 검증 유틸(JWKS/dev-secret) 필요.
- 서버 액션 ID 는 빌드 해시로 추정 가능 — 백엔드 `require_admin` 누락이 곧 인가 결함.
- CSP `unsafe-inline`/`unsafe-eval` 유지 — 신규 렌더 sink 추가 시 재점검.
- `/api/cli-download` 는 의도된 무인증(백엔드 `/cli` 공개와 동일 정책).
- `endpoint_url` 등 관리자 입력 URL 은 admin-api 측 SSRF 검증에 의존.
