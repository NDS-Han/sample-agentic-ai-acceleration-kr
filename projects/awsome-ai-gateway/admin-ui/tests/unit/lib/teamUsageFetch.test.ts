// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 상세의 팀 사용량 — 예전 50행 × 순차 페이지(300명 팀 = 6 요청)를 없앴는지.
 *  - totalOnly(사용자 상세): page_size=1 한 요청, 서버 scope_total 사용
 *  - 구성원 표: page_size=500 한 요청
 *  - 구 admin-api(scope_total 없음 / page_size>50 이면 422) 호환
 *  - 공유 캐시: 같은 키는 한 요청, 구독자가 모두 떠나면 진행 중 요청 abort
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { __resetSharedCache, acquireShared, fetchTeamUsage } from '@/components/users/detail/useDetailData';

const cell = (cost: number) => ({
  input_tokens: 1, output_tokens: 1, cache_read_tokens: 0, cache_write_tokens: 0,
  total_tokens: 2, cost_usd: cost, requests: 1,
});

function respond(body: object, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

function stub(handler: (_u: URL) => Response) {
  const f = vi.fn(async (input: RequestInfo | URL) => handler(new URL(String(input), 'http://x')));
  vi.stubGlobal('fetch', f);
  return f;
}

beforeEach(() => __resetSharedCache());
afterEach(() => vi.unstubAllGlobals());

describe('fetchTeamUsage', () => {
  it('totalOnly — one request with page_size=1, total from scope_total', async () => {
    const f = stub(() => respond({ rows: [{ user_id: 'a', total: cell(1) }], page_size: 1, total_users: 300, scope_total: cell(42) }));
    const r = await fetchTeamUsage('t1', '2026-11', { totalOnly: true });
    expect(f).toHaveBeenCalledTimes(1);
    expect(new URL(String(f.mock.calls[0][0]), 'http://x').searchParams.get('page_size')).toBe('1');
    expect(r.total.cost_usd).toBe(42);
  });

  it('member table — 300 members in one request of 500', async () => {
    const rows = Array.from({ length: 300 }, (_, i) => ({ user_id: `u${i}`, total: cell(1) }));
    const f = stub(() => respond({ rows, page_size: 500, total_users: 300, scope_total: cell(300) }));
    const r = await fetchTeamUsage('t1', '2026-11');
    expect(f).toHaveBeenCalledTimes(1);
    expect(r.byUser.size).toBe(300);
    expect(r.total.cost_usd).toBe(300);
  });

  it('old admin-api — totalOnly without scope_total retries at page_size=50 and sums', async () => {
    const f = stub((u) => {
      const size = Number(u.searchParams.get('page_size'));
      const page = Number(u.searchParams.get('page'));
      return respond({ rows: [1, 2, 3].map((i) => ({ user_id: `u${size}x${page}x${i}`, total: cell(2) })), page_size: size, total_users: 3 });
    });
    const r = await fetchTeamUsage('t1', '2026-11', { totalOnly: true });
    expect(f).toHaveBeenCalledTimes(2); // page_size=1 시도 + 50으로 한 번
    expect(new URL(String(f.mock.calls[1][0]), 'http://x').searchParams.get('page_size')).toBe('50');
    expect(r.total.cost_usd).toBe(6);
  });

  it('old admin-api — 422 on page_size=500 retries with 50', async () => {
    const f = stub((u) =>
      u.searchParams.get('page_size') === '500'
        ? respond({ detail: 'page_size' }, 422)
        : respond({ rows: [{ user_id: 'a', total: cell(1) }], page_size: 50, total_users: 1 }),
    );
    const r = await fetchTeamUsage('t1', '2026-11');
    expect(f).toHaveBeenCalledTimes(2);
    expect(r.byUser.size).toBe(1);
  });
});

describe('acquireShared', () => {
  it('dedupes concurrent and recent requests for the same key', async () => {
    const fn = vi.fn(async () => 7);
    const a = acquireShared('k', fn);
    const b = acquireShared('k', fn);
    expect(await a.promise).toBe(7);
    expect(await b.promise).toBe(7);
    a.release();
    b.release();
    const c = acquireShared('k', fn);
    expect(await c.promise).toBe(7);
    expect(fn).toHaveBeenCalledTimes(1);
  });

  it('aborts the in-flight request when the last subscriber leaves, and does not cache it', async () => {
    let seen: AbortSignal | null = null;
    const fn = vi.fn((signal: AbortSignal) => {
      seen = signal;
      return new Promise<number>((_, reject) => signal.addEventListener('abort', () => reject(new Error('aborted'))));
    });
    const a = acquireShared('k2', fn);
    const b = acquireShared('k2', fn);
    a.promise.catch(() => {});
    a.release();
    expect(seen!.aborted).toBe(false); // b 가 아직 기다린다
    b.release();
    expect(seen!.aborted).toBe(true);
    const fn2 = vi.fn(async () => 1);
    expect(await acquireShared('k2', fn2).promise).toBe(1);
    expect(fn2).toHaveBeenCalledTimes(1);
  });
});
