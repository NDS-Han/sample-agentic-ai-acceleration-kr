// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * middleware — 만료 분기가 실제로 배선돼 있는지(순수 함수 테스트만으로는 증명 안 됨).
 *
 * 회귀: 만료된 admin_jwt 는 "쿠키가 있다"는 이유로 미인증 분기를 타지 않아 페이지가
 * 그대로 열렸고, 서버 컴포넌트의 admin-api 호출만 전부 401 이 됐다. 사용자에게는
 * 로그인으로 돌아갈 길이 없었다.
 */

import { describe, it, expect } from 'vitest';
import { NextRequest } from 'next/server';
import { middleware } from '@/middleware';

function b64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

function tokenWithExp(offsetSeconds: number): string {
  return `header.${b64url({
    sub: '11111111-1111-1111-1111-111111111111',
    role: 'ADMIN',
    exp: Math.floor(Date.now() / 1000) + offsetSeconds,
  })}.sig`;
}

const DEV_TOKEN = `dev.${b64url({
  user_id: 'dev-admin',
  email: 'admin@dev.local',
  display_name: 'Dev Admin',
  role: 'ADMIN',
  team_id: null,
  department_id: null,
  issued_at: new Date().toISOString(),
  expires_at: new Date(Date.now() + 86_400_000).toISOString(),
})}.sig`;

function requestWith(cookie?: string, pathname = '/'): NextRequest {
  const req = new NextRequest(`http://admin.test${pathname}`);
  if (cookie !== undefined) {
    req.cookies.set('admin_jwt', cookie);
  }
  return req;
}

// ⚠️ 리다이렉트 목적지는 `/api/auth/dev-login` 이 아니라 `/api/auth/login` 이다.
//    prod 는 `DEV_LOGIN_ENABLED=false` 라 dev-login GET 이 404 였고, admin_jwt 를 굽는
//    코드가 레포에 dev-login 뿐이어서 **prod 에는 로그인 경로가 아예 없었다**(미인증
//    브라우저 → 307 → 바디 없는 404 막다른 길). 이제 middleware 는 환경을 보고
//    OIDC authorize / dev 폼 / 읽히는 503 으로 갈라주는 단일 진입점 `/api/auth/login`
//    으로 보낸다(src/app/api/auth/login/route.ts).
/**
 * 같은 오리진 리다이렉트의 계약: Location 은 **요청의 Host + x-forwarded-proto 로 만든
 * 절대 URL** 이고 경로만 다르다.
 *
 * 왜 상대 경로가 아닌가: Next.js 14.2 미들웨어 어댑터가 Location 을 `new NextURL()` 로
 * 다시 해석해 상대 경로면 `Invalid URL` 로 500 이 난다(라우트 핸들러는 무관). 왜
 * request.url 이 아닌가: 컨테이너 안에서 0.0.0.0 으로 풀린다. 그래서 login/callback
 * 라우트와 같은 헤더 규칙을 쓴다 — 테스트 요청의 Host 는 admin.test, proto 는 http.
 */
function expectSameOriginRedirect(res: Response, path: string): void {
  const loc = res.headers.get('location');
  expect(loc).not.toBeNull();
  const url = new URL(loc!);
  expect(url.host).toBe('admin.test');
  expect(url.protocol).toBe('http:');
  expect(url.pathname).toBe(path);
  // 절대 URL 이라도 `//host` 스킴 상대 형태로 새지 않는다.
  expect(loc!.startsWith('//')).toBe(false);
}

describe('middleware — session expiry', () => {
  it('redirects an EXPIRED token to login and clears the stale cookie', async () => {
    const res = await middleware(requestWith(tokenWithExp(-3600)));

    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
    // 못 쓰는 자격증명은 응답에서 제거돼야 한다.
    const setCookie = res.headers.get('set-cookie') ?? '';
    expect(setCookie).toContain('admin_jwt=');
    expect(setCookie).toMatch(/Max-Age=0|Expires=Thu, 01 Jan 1970/);
  });

  it('lets a LIVE token through', async () => {
    const res = await middleware(requestWith(tokenWithExp(3600)));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('lets a dev-login token through — it carries no numeric exp', async () => {
    const res = await middleware(requestWith(DEV_TOKEN));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('lets a token with NO expiry claim at all through (진짜 fail-open 분기)', async () => {
    // ⚠️ 위의 dev-login 테스트는 이 분기를 덮지 못한다. DEV_TOKEN 에는 ISO `expires_at` 이
    //    24시간 뒤로 들어 있어서 isSessionExpired 의 **ISO 파싱 분기**를 타고 false 가 된다.
    //    exp/expires_at 이 아예 없을 때의 관용(`if (!raw) return false`)은 그 테스트가
    //    통과해도 배선되지 않았을 수 있다 — 그러면 그런 토큰을 들고 온 사용자는 로그인
    //    직후 다시 로그인으로 튕겨 무한 리다이렉트에 빠진다. 그 분기를 직접 찍는다.
    const res = await middleware(requestWith(`dev.${b64url({ role: 'ADMIN' })}.sig`));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('lets a token with an UNPARSEABLE expiry through — 판정 보류', async () => {
    // 만료를 확신할 수 없을 때 튕겨내면 정상 사용자를 잠글 수 있다.
    const res = await middleware(
      requestWith(`dev.${b64url({ role: 'ADMIN', expires_at: 'not-a-date' })}.sig`),
    );
    expect(res.status).toBe(200);
  });

  it('redirects when the expiry is inside the skew window (UI 가 먼저 만료로 본다)', async () => {
    // admin-api 의 jose leeway 는 0 이라(admin-api/src/app/core/auth.py:63) exp 시점에
    // 정확히 401 이 된다. UI 가 그보다 늦게 만료로 보면 그 구간에서 '페이지는 열리는데
    // 전부 401' 이라는 원래 증상이 재현된다. 10초 남은 토큰은 이미 로그인으로 보내야 한다.
    const res = await middleware(requestWith(tokenWithExp(10)));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
  });

  it('still redirects when no cookie is present (and does not clear anything)', async () => {
    const res = await middleware(requestWith(undefined));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
  });

  it('redirects a malformed token to login and clears the cookie', async () => {
    const res = await middleware(requestWith('not-a-jwt'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
    expect(res.headers.get('set-cookie') ?? '').toContain('admin_jwt=');
  });

  it('applies security headers to the expiry redirect too', async () => {
    const res = await middleware(requestWith(tokenWithExp(-3600)));
    expect(res.headers.get('X-Frame-Options')).toBe('DENY');
    expect(res.headers.get('X-Content-Type-Options')).toBe('nosniff');
  });

  it('403 stays public — an expired cookie must not bounce it into a redirect loop', async () => {
    const res = await middleware(requestWith(tokenWithExp(-3600), '/403'));
    expect(res.status).toBe(200);
  });
});

describe('middleware — /cli is not public', () => {
  // 회귀: pathname.startsWith('/cli') 가 공개 분기에 있어 PAGE_PERMISSIONS
  // (ADMIN/TEAM_LEADER) 검사를 우회했고, 미인증 요청에도 페이지가 열렸다.
  // 바이너리 다운로드는 /api/cli-download 프록시('/api/' 항목)가 담당한다.
  it('redirects an unauthenticated /cli request to login', async () => {
    const res = await middleware(requestWith(undefined, '/cli'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
  });

  it('redirects an unauthenticated /cli subpath too', async () => {
    const res = await middleware(requestWith(undefined, '/cli/download/linux/x64'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/api/auth/login');
  });

  it('lets an ADMIN token through to /cli', async () => {
    const res = await middleware(requestWith(DEV_TOKEN, '/cli'));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('keeps /api/cli-download public — curl 설치 명령이 쿠키 없이 도달해야 한다', async () => {
    const res = await middleware(requestWith(undefined, '/api/cli-download/linux/x64'));
    expect(res.status).toBe(200);
  });

  it('redirects a DEVELOPER token to /403 — PAGE_PERMISSIONS 는 ADMIN+TEAM_LEADER', async () => {
    const devJwt = `header.${b64url({
      sub: '22222222-2222-2222-2222-222222222222',
      role: 'DEVELOPER',
      exp: Math.floor(Date.now() / 1000) + 3600,
    })}.sig`;
    const res = await middleware(requestWith(devJwt, '/cli'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/403');
  });
});

describe('middleware — admin_role 보조 쿠키', () => {
  // IdP id_token 에는 role 클레임이 없다 — 콜백이 admin-api /admin/my/profile 의
  // 유효 역할을 admin_role 쿠키로 굽고, middleware 는 쿠키 → 클레임 순으로 읽는다.
  const IDP_TOKEN_NO_ROLE = `header.${b64url({
    sub: '33333333-3333-3333-3333-333333333333',
    exp: Math.floor(Date.now() / 1000) + 3600,
  })}.sig`;

  function requestWithRole(role: string | undefined, pathname = '/'): NextRequest {
    const req = new NextRequest(`http://admin.test${pathname}`);
    req.cookies.set('admin_jwt', IDP_TOKEN_NO_ROLE);
    if (role !== undefined) {
      req.cookies.set('admin_role', role);
    }
    return req;
  }

  it('role 없는 IdP 토큰 + admin_role=TEAM_LEADER → / 통과', async () => {
    const res = await middleware(requestWithRole('TEAM_LEADER', '/'));
    expect(res.status).toBe(200);
    expect(res.headers.get('location')).toBeNull();
  });

  it('admin_role=DEVELOPER → / 는 403, /my 는 통과', async () => {
    const root = await middleware(requestWithRole('DEVELOPER', '/'));
    expect(root.status).toBe(307);
    expectSameOriginRedirect(root, '/403');

    const mine = await middleware(requestWithRole('DEVELOPER', '/my'));
    expect(mine.status).toBe(200);
  });

  it('admin_role 없는 IdP 토큰 → 역할 모름 → / 는 403(fail-closed)', async () => {
    const res = await middleware(requestWithRole(undefined, '/'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/403');
  });

  it('조작된 admin_role 값 → 알 수 없는 역할 → fail-closed', async () => {
    const res = await middleware(requestWithRole('SUPERADMIN', '/'));
    expect(res.status).toBe(307);
    expectSameOriginRedirect(res, '/403');
  });
});
