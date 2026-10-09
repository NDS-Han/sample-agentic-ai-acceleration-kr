// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * chat-proxy catch-all 경로 화이트리스트 회귀 (R3-2).
 *
 * 배경: `/api/chat-proxy/[...path]` 의 세그먼트를 그대로 join 하던 구현은
 * `..`/`%2e%2e` 가 URL 정규화로 상위 디렉터리를 빠져나가 — 라이브 dev 에서
 * `/api/chat-proxy/admin/chat/%2e%2e/%2e%2e/internal/cache/retry` 요청이
 * 실제 admin-api `/internal/cache/retry` 에 도달하는 것이 확인됐다. 사용자의
 * admin_jwt 가 임의 admin-api 경로로 전달됐다.
 *
 * 고정하는 것:
 *   1. 허용 경로 `admin/chat/...` 만 상류 fetch 에 도달한다.
 *   2. out-of-scope 경로(admin/users 등)는 fetch 전에 400.
 *   3. traversal 세그먼트(`..`, `%2e%2e`, `.`, 빈, `%`, `\`)는 400.
 */

// @vitest-environment node

import { describe, it, expect, vi, afterEach, beforeEach } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('next/headers', () => ({
  cookies: () => ({
    get: (name: string) =>
      name === 'admin_jwt' ? { value: 'test-jwt' } : undefined,
  }),
}));

import { GET } from '@/app/api/chat-proxy/[...path]/route';

function makeReq(path: string) {
  return new NextRequest(`http://admin.test/api/chat-proxy/${path}`);
}

const upstreamOk = () =>
  Promise.resolve(
    new Response('ok', { status: 200, headers: { 'content-type': 'text/plain' } }),
  );

beforeEach(() => {
  vi.stubGlobal('fetch', vi.fn(upstreamOk));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

const param = (...parts: string[]) => ({ params: { path: parts } });

describe('chat-proxy path whitelist', () => {
  it('forwards allowed admin/chat paths upstream', async () => {
    const res = await GET(
      makeReq('admin/chat/models'),
      param('admin', 'chat', 'models'),
    );
    expect(res.status).toBe(200);
    expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining('/admin/chat/models'),
      expect.anything(),
    );
  });

  it('rejects out-of-scope admin paths', async () => {
    for (const parts of [
      ['admin', 'users', 'search'],
      ['internal', 'cache', 'retry'],
      ['admin', 'chat'],
    ] as const) {
      const res = await GET(makeReq(parts.join('/')), param(...parts));
      expect(res.status).toBe(400);
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it('rejects traversal and encoded segments', async () => {
    const cases: string[][] = [
      ['admin', 'chat', '..', '..', 'internal'],
      ['admin', 'chat', '%2e%2e', 'internal'],
      ['admin', 'chat', '.', 'models'],
      ['admin', 'chat', 'x%25y'],
      ['admin', 'chat', 'a\\b'],
      ['admin', 'chat', ''],
      // R4A1-1: Next.js 가 `%2f` 를 세그먼트 "내부"의 `/` 로 디코딩해 전달한다.
      // `..%2finternal` → `../internal` — join('/') 이 되살려 경로 구분자로
      // 부활하므로 세그먼트 내 슬래시는 거절한다.
      ['admin', 'chat', '../internal', 'cache', 'retry'],
      ['admin', 'chat', 'foo/bar'],
      // URL 정규화 탈출을 한 번 더 막는 심층방어(buildTargetUrl)의 먹이:
      // 세그먼트 검사를 통과해도 결과 경로가 /admin/chat/ 밖이면 400.
      ['admin', 'chat', '..', 'users'],
    ];
    for (const parts of cases) {
      const res = await GET(makeReq(parts.join('/')), param(...parts));
      expect(res.status).toBe(400);
    }
    expect(fetch).not.toHaveBeenCalled();
  });
});
