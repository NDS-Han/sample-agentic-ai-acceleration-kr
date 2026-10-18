// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * rate-limits BFF 프록시의 경로 세그먼트 화이트리스트 회귀 (R25-N-C11).
 *
 * 배경: `/api/rate-limits/usage|usage-trend` 는 `?scope=&scope_id=` 쿼리를 상류
 * URL 경로 세그먼트로 그대로 합쳤다. `encodeURIComponent('..')` 는 '..' 그대로라
 * `/admin/rate-limits/usage/../x` 가 경로 정규화로 탈출해 admin-api 의 임의
 * 경로에 admin_jwt 가 딸려 도달할 수 있었다. SAFE_SEGMENT 문자셋 가드로 고쳤다.
 *
 * 고정하는 것:
 *   1. '..' · `/` · `#` 등 허용 문자셋 밖 세그먼트는 fetch 전에 400.
 *   2. 누락 파라미터는 400.
 *   3. 유효 파라미터는 상류로 전달 + admin_jwt 쿠키 첨부.
 *   4. 상류 401 은 401, 나머지 상류 오류/fetch 실패는 fail-soft 200.
 */

// @vitest-environment node

import { describe, it, expect, vi, afterEach } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('next/headers', () => ({
  cookies: () => ({
    get: (name: string) =>
      name === 'admin_jwt' ? { value: 'test-jwt' } : undefined,
  }),
}));

import { GET as usageGET } from '@/app/api/rate-limits/usage/route';
import { GET as trendGET } from '@/app/api/rate-limits/usage-trend/route';

function req(path: string, params: Record<string, string>) {
  const qs = new URLSearchParams(params).toString();
  return new NextRequest(`http://admin.test${path}?${qs}`);
}

const okJson = () =>
  Promise.resolve(
    new Response('{"available":true}', {
      status: 200,
      headers: { 'content-type': 'application/json' },
    }),
  );

function fetchSpy(impl: typeof fetch = okJson as typeof fetch) {
  const spy = vi.fn(impl);
  vi.stubGlobal('fetch', spy);
  return spy;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

const VALID = { scope: 'user', scope_id: 'a-b_c9' };

describe('GET /api/rate-limits/usage', () => {
  it('허용 문자셋 밖 scope/scope_id 는 상류를 호출하지 않고 400', async () => {
    const spy = fetchSpy();
    for (const params of [
      { scope: '..', scope_id: 'x' },
      { scope: 'user', scope_id: '..' },
      { scope: 'user', scope_id: 'a/b' },
      { scope: 'user', scope_id: 'a#b' },
      { scope: 'team%20x', scope_id: 'y' },
      { scope: '.', scope_id: 'y' },
    ]) {
      const res = await usageGET(req('/api/rate-limits/usage', params));
      expect(res.status).toBe(400);
      const body = await res.json();
      expect(body.available).toBe(false);
    }
    expect(spy).not.toHaveBeenCalled();
  });

  it('누락 파라미터는 400', async () => {
    const spy = fetchSpy();
    for (const params of [
      {},
      { scope: 'user' },
      { scope_id: 'abc' },
    ] as Record<string, string>[]) {
      const res = await usageGET(req('/api/rate-limits/usage', params));
      expect(res.status).toBe(400);
    }
    expect(spy).not.toHaveBeenCalled();
  });

  it('유효 파라미터는 상류 경로에 인코딩해 전달하고 admin_jwt 를 쿠키로 붙인다', async () => {
    const spy = fetchSpy();
    const res = await usageGET(req('/api/rate-limits/usage', VALID));
    expect(res.status).toBe(200);

    const callArgs = spy.mock.calls[0] as unknown as [string | URL, RequestInit];
    expect(String(callArgs[0])).toContain('/admin/rate-limits/usage/user/a-b_c9');
    expect((callArgs[1].headers as Record<string, string>)['Cookie']).toBe(
      'admin_jwt=test-jwt',
    );
  });

  it('상류 401 은 fail-soft 가 아니라 401 로 전파한다(세션 만료 신호)', async () => {
    fetchSpy(async () => new Response('nope', { status: 401 }));
    const res = await usageGET(req('/api/rate-limits/usage', VALID));
    expect(res.status).toBe(401);
    const body = await res.json();
    expect(body.available).toBe(false);
  });

  it('상류 5xx 는 fail-soft 200 + available:false', async () => {
    fetchSpy(async () => new Response('err', { status: 503 }));
    const res = await usageGET(req('/api/rate-limits/usage', VALID));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.available).toBe(false);
    expect(body.reason).toContain('503');
  });

  it('상류 fetch 자체가 실패해도 fail-soft 200', async () => {
    fetchSpy(async () => {
      throw new Error('ECONNREFUSED');
    });
    const res = await usageGET(req('/api/rate-limits/usage', VALID));
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.available).toBe(false);
  });
});

describe('GET /api/rate-limits/usage-trend', () => {
  it('허용 문자셋 밖 세그먼트는 상류를 호출하지 않고 400', async () => {
    const spy = fetchSpy();
    for (const params of [
      { scope: '..', scope_id: 'x' },
      { scope: 'user', scope_id: 'a/b' },
      { scope: 'user', scope_id: 'a\\b' },
    ]) {
      const res = await trendGET(
        req('/api/rate-limits/usage-trend', { ...params, window: '24h' }),
      );
      expect(res.status).toBe(400);
    }
    expect(spy).not.toHaveBeenCalled();
  });

  it('유효 파라미터는 window 와 함께 상류로 전달한다', async () => {
    const spy = fetchSpy();
    const res = await trendGET(
      req('/api/rate-limits/usage-trend', { ...VALID, window: '7d' }),
    );
    expect(res.status).toBe(200);
    const callArgs = spy.mock.calls[0] as unknown as [string | URL, RequestInit];
    expect(String(callArgs[0])).toContain(
      '/admin/rate-limits/usage-trend/user/a-b_c9?window=7d',
    );
  });

  it('허용 목록 밖 window 는 24h 로 정규화한다', async () => {
    const spy = fetchSpy();
    await trendGET(
      req('/api/rate-limits/usage-trend', { ...VALID, window: '99y' }),
    );
    const callArgs = spy.mock.calls[0] as unknown as [string | URL, RequestInit];
    expect(String(callArgs[0])).toContain('window=24h');
  });

  it('상류 401 은 401 로 전파한다', async () => {
    fetchSpy(async () => new Response('nope', { status: 401 }));
    const res = await trendGET(
      req('/api/rate-limits/usage-trend', { ...VALID, window: '1h' }),
    );
    expect(res.status).toBe(401);
  });

  it('정상 응답 본문을 그대로 전달한다', async () => {
    fetchSpy();
    const res = await trendGET(
      req('/api/rate-limits/usage-trend', { ...VALID, window: '6h' }),
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ available: true });
  });
});
