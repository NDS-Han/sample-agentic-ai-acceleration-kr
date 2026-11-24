// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 사용자 × 모델 프록시 라우트 — 파라미터 허용 목록 전달, user_id UUID 검증,
 * 상류 오류 상태·본문 보존(400/403/404/422), 그리고 export 의 view 화이트리스트.
 */

// @vitest-environment node

import { afterEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';

vi.mock('next/headers', () => ({
  cookies: () => ({
    get: (name: string) => (name === 'admin_jwt' ? { value: 'stub-token' } : undefined),
  }),
}));

import { GET as listGET } from '@/app/api/analytics/user-models/route';
import { GET as detailGET } from '@/app/api/analytics/user-models/[userId]/route';
import { GET as exportGET } from '@/app/api/analytics-export/route';

const UID = '6f1c0b4e-8a1d-4c2e-9f3a-0b1c2d3e4f50';

function stubFetch(status: number, body: unknown) {
  const fn = vi.fn(async () =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } }),
  );
  vi.stubGlobal('fetch', fn);
  return fn;
}

function calledUrl(fn: ReturnType<typeof vi.fn>): URL {
  return new URL(String((fn.mock.calls[0] as unknown[])[0]));
}

afterEach(() => vi.unstubAllGlobals());

describe('GET /api/analytics/user-models', () => {
  it('허용 파라미터만 상류로, 쿠키·no-store 동반', async () => {
    const fn = stubFetch(200, { rows: [] });
    const res = await listGET(
      new NextRequest(
        'http://admin.test/api/analytics/user-models?period=2026-11&start_date=2026-11-01&end_date=2026-11-10' +
          '&scope=all&client=codex&sort=model:claude-sonnet-4-6&dir=asc&page=2&page_size=25&q=kim&evil=1',
      ),
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ rows: [] });
    const url = calledUrl(fn);
    expect(url.pathname).toBe('/admin/analytics/user-models');
    expect(Object.fromEntries(url.searchParams)).toEqual({
      period: '2026-11',
      start_date: '2026-11-01',
      end_date: '2026-11-10',
      scope: 'all',
      client: 'codex',
      sort: 'model:claude-sonnet-4-6',
      dir: 'asc',
      page: '2',
      page_size: '25',
      q: 'kim',
    });
    const init = (fn.mock.calls[0] as unknown[])[1] as RequestInit;
    expect(init.cache).toBe('no-store');
    expect((init.headers as Record<string, string>).Cookie).toBe('admin_jwt=stub-token');
  });

  it.each([400, 403, 404, 422])('상류 %i 는 상태와 JSON 본문을 그대로', async (status) => {
    const body = { error: { type: 'validation_error', message: 'x', code: 'date_range_too_long' } };
    stubFetch(status, body);
    const res = await listGET(new NextRequest('http://admin.test/api/analytics/user-models?period=2026-11'));
    expect(res.status).toBe(status);
    expect(await res.json()).toEqual(body);
  });

  it('401 은 기계 판독 JSON, 5xx 는 502', async () => {
    stubFetch(401, {});
    const r1 = await listGET(new NextRequest('http://admin.test/api/analytics/user-models'));
    expect(r1.status).toBe(401);
    expect(await r1.json()).toMatchObject({ error_code: 'UNAUTHORIZED' });
    stubFetch(500, {});
    const r2 = await listGET(new NextRequest('http://admin.test/api/analytics/user-models'));
    expect(r2.status).toBe(502);
  });

  it('상류 연결 실패는 502', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('fetch failed'); }));
    const res = await listGET(new NextRequest('http://admin.test/api/analytics/user-models'));
    expect(res.status).toBe(502);
  });
});

describe('GET /api/analytics/user-models/[userId]', () => {
  const ctx = (userId: string) => ({ params: Promise.resolve({ userId }) });

  it('UUID 가 아니면 400, 상류 호출 없음', async () => {
    const fn = stubFetch(200, {});
    for (const bad of ['abc', '..%2Fadmin', `${UID}x`]) {
      const res = await detailGET(new NextRequest('http://admin.test/api/analytics/user-models/x'), ctx(bad));
      expect(res.status).toBe(400);
    }
    expect(fn).not.toHaveBeenCalled();
  });

  it('필터 파라미터만 전달(sort/page 등은 버림)', async () => {
    const fn = stubFetch(200, { user_id: UID, models: [] });
    const res = await detailGET(
      new NextRequest(
        `http://admin.test/api/analytics/user-models/${UID}?period=2026-11&scope=team:t1&client=cowork&sort=name&page=3`,
      ),
      ctx(UID),
    );
    expect(res.status).toBe(200);
    const url = calledUrl(fn);
    expect(url.pathname).toBe(`/admin/analytics/user-models/${UID}`);
    expect(Object.fromEntries(url.searchParams)).toEqual({ period: '2026-11', scope: 'team:t1', client: 'cowork' });
  });

  it('404 는 그대로', async () => {
    stubFetch(404, { error: { message: 'not found' } });
    const res = await detailGET(new NextRequest(`http://admin.test/api/analytics/user-models/${UID}`), ctx(UID));
    expect(res.status).toBe(404);
    expect(await res.json()).toEqual({ error: { message: 'not found' } });
  });
});

describe('analytics-export view 화이트리스트', () => {
  function csvFetch() {
    const fn = vi.fn(async () => new Response('a,b\n', { status: 200, headers: { 'content-type': 'text/csv' } }));
    vi.stubGlobal('fetch', fn);
    return fn;
  }
  it('view=user_model 은 전달', async () => {
    const fn = csvFetch();
    await exportGET(new NextRequest('http://admin.test/api/analytics-export?period=2026-11&format=csv&view=user_model'));
    expect(calledUrl(fn).searchParams.get('view')).toBe('user_model');
  });
  it('그 외 view 는 버린다', async () => {
    const fn = csvFetch();
    await exportGET(new NextRequest('http://admin.test/api/analytics-export?period=2026-11&format=csv&view=raw'));
    expect(calledUrl(fn).searchParams.has('view')).toBe(false);
    expect(calledUrl(fn).searchParams.get('format')).toBe('csv');
  });
});
