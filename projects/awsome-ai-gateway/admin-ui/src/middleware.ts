// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Next.js Middleware — SEC-01 pattern.
 *
 * Applies security headers to all responses and enforces JWT-based
 * authentication for non-API page routes.
 */

import { NextRequest, NextResponse } from 'next/server';
import { parseJWT, roleFromCookie, ADMIN_ROLE_COOKIE } from '@/lib/auth';
import { checkPagePermission, isSessionExpired } from '@/lib/auth';

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon\\.ico).*)'],
};

// kiro R17-6 N2 — env 값은 admin 설정이지만 검증은 한다: `;` 로 다른 디렉티브
// 주입, `*` 와일드카드(S1 재개방), scheme 포함 값을 전부 걸러낸다.
// ⚠️ 모듈 초기화 순서: SECURITY_HEADERS 초기화가 contentSecurityPolicy() 를
//    호출하므로 이 상수는 그보다 위에 있어야 한다(const 는 TDZ).
const CSP_HOST_RE = /^[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+(:\d+)?$/;
// kiro R17-7 N-2 — 호스트 형식만 검사하면 `s3.ap-south-1.amazonaws.com` 같은
// path-style 리전 엔드포인트가 통과해 모든 버킷 경로가 허용돼 S1 유출 경로가
// 다시 열린다. `s3.`/`s3-` 로 시작하는 호스트와 VPC endpoint 호스트
// (`vpce-…​.s3.리전.vpce.amazonaws.com` — kiro R17-8 N-e, 경로로 임의 버킷
// 지정 가능)는 거부 — virtual-hosted(버킷.s3.리전.amazonaws.com)만 허용.
const CSP_HOST_PATHSTYLE_S3_RE = /^s3[.-]|^vpce-/i;

const SECURITY_HEADERS: Record<string, string> = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
  // R17-S7 — CSP 는 여기 한 곳에서만 정의한다. next.config.mjs 의 headers() 에도
  // 따로 있었는데, 두 정책이 같이 내려가면 브라우저는 교집합을 적용해 어느 쪽이
  // 유효한지 추적이 안 된다(그리고 그쪽 img-src 에는 S3 presigned 차트 이미지를
  // 허용하는 규칙이 없어 kind:'image' 차트가 항상 차단됐다).
  //   img-src … CHAT_IMAGE_HOSTS(쉼표 구분 호스트) — report/chart 의 presigned
  //   S3 URL 용. `*.amazonaws.com` 와일드카드는 공격자 소유 S3/execute-api 도
  //   허용해 LLM 마크다운 `![…](https://attacker…/?d=유출)` 경유 exfil 을 연다
  //   (kiro R17-5 S1) — 정확한 버킷 호스트만 환경변수로 받는다. 미설정이면
  //   외부 이미지 전면 차단(fail-closed — chat-agent 미배포 환경 기본).
  //   ⚠️ middleware env 는 self-hosted 에서 런타임 평가 — 값 변경은 재배포만으로 반영.
  //   unsafe-inline/-eval 는 Next 의 인라인 스크립트 구조상 즉시 제거 불가 —
  //   nonce 기반 strict-dynamic 은 Next 15.5+ 라 가능하지만 next-intl/차트
  //   라이브러리의 인라인 스크립트까지 nonce 도배가 필요해 별도 작업으로 분리.
  'Content-Security-Policy': contentSecurityPolicy(),
};

function contentSecurityPolicy(): string {
  const imgHosts = (process.env.CHAT_IMAGE_HOSTS ?? '')
    .split(',')
    .map((h) => h.trim())
    .filter((h) => CSP_HOST_RE.test(h) && !CSP_HOST_PATHSTYLE_S3_RE.test(h))
    .map((h) => `https://${h}`)
    .join(' ');
  return (
    "default-src 'self'; script-src 'self' 'unsafe-eval' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; " +
    `img-src 'self' data: blob:${imgHosts ? ` ${imgHosts}` : ''}; ` +
    "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'"
  );
}

function applySecurityHeaders(response: NextResponse): NextResponse {
  for (const [key, value] of Object.entries(SECURITY_HEADERS)) {
    response.headers.set(key, value);
  }
  return response;
}

/**
 * 로그인 화면으로 되돌린다. `clearCookie` 면 남아 있는 admin_jwt 도 지운다.
 *
 * nextUrl 을 쓰는 이유: request.url 은 Docker 안에서 0.0.0.0 으로 풀려 원래 호스트를 잃는다.
 *
 * ⚠️ 목적지가 `/api/auth/dev-login` 이 아니라 `/api/auth/login` 인 이유(A3):
 *    dev-login 라우트는 DEV_LOGIN_ENABLED !== 'true' 이면 본문 없는 404 를 준다. prod 는
 *    그 값이 "false"(values-eks-fargate-prod.yaml adminUi.env) 이므로 예전 목적지로는
 *    **prod 브라우저가 307 → 빈 404 에서 끝났다** — ALB 인증도 없어 로그인 경로가 0개였다.
 *    `/api/auth/login` 은 환경을 보고 OIDC authorize / dev 폼 / 읽히는 503 으로 갈라주는
 *    단일 진입점이다(app/api/auth/login/route.ts). 이 경로는 아래 `/api/` 예외에 걸려
 *    미인증으로도 도달 가능하다 — 그래야 무한 리다이렉트가 안 난다.
 */
/**
 * 미들웨어의 리다이렉트는 **절대 URL** 이어야 한다. Next.js 14.2 의 미들웨어 어댑터가
 * 응답의 Location 을 `new NextURL(location)` 으로 다시 해석하는데(next/dist/server/web/
 * adapter.js) 상대 경로에는 base 가 없어 `TypeError: Invalid URL` 이 나고, 미인증·만료
 * 요청이 오는 **모든 페이지가 500** 이 된다(2026-09-15 US dev 실측). 라우트 핸들러
 * (app/api/*)의 redirectRelative 는 그 어댑터를 타지 않으므로 그대로 둔다.
 *
 * 오리진은 request.url(컨테이너 안에서 0.0.0.0/localhost 로 풀린다)이 아니라 login·
 * callback·logout 라우트와 같은 헤더 규칙으로 만든다 — x-forwarded-proto 의 첫 토큰
 * + Host. CloudFront 뒤에서 Host 가 ALB 이름으로 오는 구성은 redirect_uri 도 같은 값을
 * 쓰므로 이미 viewer Host 전달이 전제다.
 */
function externalOrigin(request: NextRequest): string {
  const host = request.headers.get('host') || request.nextUrl.host || 'localhost:3000';
  const rawProto =
    request.headers.get('x-forwarded-proto') || request.nextUrl.protocol.replace(/:$/, '') || 'http';
  const proto = rawProto.split(',')[0].trim() || 'http';
  return `${proto}://${host}`;
}

/** 같은 오리진 안의 경로로만 보낸다 — `//evil.com`·`/\evil.com` 은 스킴 상대 URL 이라 거부. */
function redirectSameOrigin(request: NextRequest, path: string, status: number): NextResponse {
  if (!path.startsWith('/') || path.startsWith('//') || path.startsWith('/\\')) {
    throw new Error(`redirectSameOrigin: same-origin path expected, got ${path}`);
  }
  return NextResponse.redirect(new URL(path, externalOrigin(request)), status);
}

function redirectToLogin(request: NextRequest, clearCookie: boolean): NextResponse {
  const redirectResponse = redirectSameOrigin(request, '/api/auth/login', 307);
  if (clearCookie) {
    // 만료/손상된 자격증명은 응답에서 즉시 제거한다 — 안 지우면 다음 요청도 같은 쿠키로
    // 다시 이 분기를 타고, 사용자는 못 쓰는 쿠키를 계속 들고 다닌다.
    redirectResponse.cookies.delete('admin_jwt');
    redirectResponse.cookies.delete(ADMIN_ROLE_COOKIE);
  }
  applySecurityHeaders(redirectResponse);
  return redirectResponse;
}

/**
 * Origin 기반 CSRF 차단 — 변형 메서드 전용.
 *
 * admin_jwt 는 SameSite=Lax 라 크로스사이트 POST 에 브라우저가 쿠키를 안 실어
 * 주는 게 1차 방어다. 여기서 한 겹 더: 크로스오리진 fetch·폼 POST 에는 브라우저가
 * 반드시 Origin(또는 구형 클라이언트는 Referer)을 실어내므로, 그 호스트가
 * 우리 Host 와 다르면 거절한다. curl 같은 비브라우저 클라이언트는 Origin 을
 * 안 보내는데 그건 피해자 브라우저를 탈취하는 CSRF 위협 모델 밖이라 허용한다.
 * GET/HEAD 는 대상이 아니다 — 브라우저가 top-level GET 에도 쿠키를 싣는 게
 * Lax 의 의도된 동작이고, 이 서버는 GET 에 부수효과를 두지 않는다.
 */
const MUTATING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

function crossOriginRejected(request: NextRequest): NextResponse | null {
  if (!MUTATING_METHODS.has(request.method)) return null;

  const host = request.headers.get('host')?.toLowerCase();
  const origin = request.headers.get('origin');
  if (origin) {
    try {
      if (new URL(origin).host.toLowerCase() === host) return null;
    } catch {
      /* 파싱 불가 Origin — 아래에서 거절 */
    }
    return NextResponse.json(
      { error: 'cross-origin request rejected' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  const referer = request.headers.get('referer');
  if (referer) {
    try {
      if (new URL(referer).host.toLowerCase() === host) return null;
    } catch {
      /* 파싱 불가 Referer — 아래에서 거절 */
    }
    return NextResponse.json(
      { error: 'cross-origin request rejected' },
      { status: 403, headers: { 'Cache-Control': 'no-store' } },
    );
  }
  return null;
}

export async function middleware(request: NextRequest): Promise<NextResponse> {
  const { pathname } = request.nextUrl;

  // CSRF — 변형 요청의 크로스오리진 발신을 먼저 끊는다 (인증 쿠키 존재 여부와 무관).
  const csrfReject = crossOriginRejected(request);
  if (csrfReject) return applySecurityHeaders(csrfReject);

  // Always start with a pass-through response so we can attach headers
  const response = NextResponse.next();
  applySecurityHeaders(response);

  // Public routes — no auth required.
  // '/403' must be public, otherwise an authenticated user without permission
  // for the current path gets redirected to /403, which itself fails the
  // permission check, and bounces back to /403 → ERR_TOO_MANY_REDIRECTS.
  // '/cli' 페이지는 예외가 아니다 — CLI 바이너리 다운로드는 /api/cli-download
  // 프록시('/api/' 항목)가 담당하므로 페이지 자체는 PAGE_PERMISSIONS(ADMIN/
  // TEAM_LEADER) 검사를 받아야 한다.
  if (
    pathname.startsWith('/api/') ||
    pathname === '/403'
  ) {
    return response;
  }

  const jwtCookie = request.cookies.get('admin_jwt');

  // No JWT present — 환경에 맞는 로그인 진입점(/api/auth/login)으로 보낸다.
  if (!jwtCookie?.value) {
    return redirectToLogin(request, false);
  }

  // JWT present — parse, check expiry, then check permissions
  try {
    const session = parseJWT(jwtCookie.value);

    // ⚠️ 만료 검사. 예전엔 이 분기가 아예 없어서 만료된 admin_jwt 로도 페이지가 열렸고,
    //    서버 컴포넌트의 모든 admin-api 호출만 401 이 됐다 — 화면 전체가 '—' 와
    //    fetchFailed 로 덮이고 로그인으로 돌아갈 방법이 없었다. 만료는 미인증과 같게
    //    취급한다(쿠키 제거 + 로그인 리다이렉트).
    //    isSessionExpired 는 exp 클레임이 없거나 해석 불가면 false 를 준다 — dev 토큰에는
    //    숫자 exp 가 없어서, 여기서 만료로 몰면 dev 로그인이 무한 리다이렉트가 된다.
    if (isSessionExpired(session)) {
      return redirectToLogin(request, true);
    }

    // 역할 판정 순서: admin_role 쿠키(admin-api 가 판정한 유효 역할 — 콜백이 구움)
    // → 토큰 role 클레임(dev/내부 JWT). IdP 토큰에는 role 이 없어서 쿠키가 없으면
    // TEAM_LEADER/DEVELOPER 를 구별할 수 없다(콜백 route.ts 주석 참조).
    const role =
      roleFromCookie(request.cookies.get(ADMIN_ROLE_COOKIE)?.value) ?? session.role;
    const hasPermission = checkPagePermission(pathname, role);

    if (!hasPermission) {
      const redirectResponse = redirectSameOrigin(request, '/403', 307);
      applySecurityHeaders(redirectResponse);
      return redirectResponse;
    }
  } catch {
    // Malformed JWT — treat as unauthenticated (그리고 못 쓰는 쿠키는 지운다)
    return redirectToLogin(request, true);
  }

  return response;
}
