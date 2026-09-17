// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Logout route — clears the admin_jwt cookie and redirects to '/login'.
 *
 * Mirrors the proto/host handling in dev-login/route.ts so the Set-Cookie
 * `secure` flag matches the actual connection scheme (HTTP vs HTTPS) and
 * the redirect URL preserves the original Host header (avoids 0.0.0.0 in
 * containerized envs). The redirect lands on '/login' — the 8-L 로그인 페이지
 * (Cognito 폼 + DEV_LOGIN_ENABLED 일 때만 dev-login 링크). It used to point
 * straight at '/api/auth/dev-login', which answers a bodyless 404 in prod.
 */

import { NextRequest, NextResponse } from 'next/server';
import { redirectRelative } from '@/lib/redirect';

export async function POST(request: NextRequest): Promise<NextResponse> {
  // host 는 더 필요 없다 — 리다이렉트가 상대 경로다(lib/redirect.ts). proto 는 쿠키의
  // Secure 플래그 판정에 여전히 쓴다(HTTP 종단에서 Secure 를 붙이면 쿠키가 저장 안 된다).
  const proto = request.headers.get('x-forwarded-proto') || 'http';

  // 상대 Location — Host 헤더가 CloudFront 뒤에서 ALB 이름일 수 있다(lib/redirect.ts).
  const response = redirectRelative('/login');
  response.cookies.set('admin_jwt', '', {
    httpOnly: true,
    sameSite: 'lax',
    path: '/',
    maxAge: 0,
    secure: proto === 'https',
  });
  return response;
}
