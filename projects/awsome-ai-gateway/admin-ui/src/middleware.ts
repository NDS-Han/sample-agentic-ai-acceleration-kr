// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Next.js Middleware — SEC-01 pattern.
 *
 * Applies security headers to all responses and enforces JWT-based
 * authentication for non-API page routes.
 */

import { NextRequest, NextResponse } from 'next/server';
import { parseJWT } from '@/lib/auth';
import { checkPagePermission, isSessionExpired } from '@/lib/auth';

export const config = {
  matcher: ['/((?!_next/static|_next/image|favicon\\.ico).*)'],
};

const SECURITY_HEADERS: Record<string, string> = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Strict-Transport-Security': 'max-age=31536000; includeSubDomains',
  'Content-Security-Policy':
    "default-src 'self'; script-src 'self' 'unsafe-eval' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; font-src 'self' data:",
};

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
  }
  applySecurityHeaders(redirectResponse);
  return redirectResponse;
}

export async function middleware(request: NextRequest): Promise<NextResponse> {
  const { pathname } = request.nextUrl;

  // Always start with a pass-through response so we can attach headers
  const response = NextResponse.next();
  applySecurityHeaders(response);

  // Public routes — no auth required.
  // '/403' must be public, otherwise an authenticated user without permission
  // for the current path gets redirected to /403, which itself fails the
  // permission check, and bounces back to /403 → ERR_TOO_MANY_REDIRECTS.
  if (
    pathname.startsWith('/api/') ||
    pathname.startsWith('/cli') ||
    pathname === '/login' ||
    pathname === '/403'
  ) {
    return response;
  }

  const jwtCookie = request.cookies.get('admin_jwt');

  // No JWT present — redirect through the single login entry point.
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

    const hasPermission = checkPagePermission(pathname, session.role);

    if (!hasPermission) {
      const redirectResponse = redirectSameOrigin(request, '/403', 307);
      applySecurityHeaders(redirectResponse);
      return redirectResponse;
    }
  } catch {
    // Malformed JWT — treat as unauthenticated
    return redirectToLogin(request, true);
  }

  return response;
}
