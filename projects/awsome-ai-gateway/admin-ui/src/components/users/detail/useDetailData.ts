'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 상세의 데이터 로더 — 기존 API 만 쓴다(+ /admin/users/activity).
 *
 *  - 구성원별 이번 달 사용: GET /api/analytics/user-models?scope=team:{id} (BFF → admin-api).
 *    사용이 없는 구성원은 행이 없다 → 0 으로 본다.
 *  - 사용자 1명의 모델별 사용: GET /api/analytics/user-models/{userId}.
 *  - 마지막 사용·웹 검색 수: getUsersActivityAction (GET /admin/users/activity).
 */

import { useCallback, useEffect, useState } from 'react';

import { getEffectivePolicyAction, getTeamEffectivePolicyAction, getUsersActivityAction, type UserActivityItem } from '@/lib/actions/users';
import { listActiveModelsAction } from '@/lib/actions/models';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { currentCalendarMonth } from '@/lib/utils/period';
import {
  fetchUserModelJson,
  UserModelFetchError,
  type UserModelCell,
  type UserModelDetail,
  type UserModelResponse,
} from '@/components/analytics/userModel';
import type { EffectivePolicy, ModelListItem } from '@/types/entities';

export type Load<T> = { status: 'loading' } | { status: 'failed' } | { status: 'ready'; data: T };

export function useCurrentPeriod(): string {
  const tz = useReportingTz();
  return currentCalendarMonth(tz);
}

export function useCatalog(): Load<ModelListItem[]> {
  const [s, setS] = useState<Load<ModelListItem[]>>({ status: 'loading' });
  useEffect(() => {
    let alive = true;
    listActiveModelsAction().then((r) => {
      if (alive) setS(r.success ? { status: 'ready', data: r.data } : { status: 'failed' });
    });
    return () => {
      alive = false;
    };
  }, []);
  return s;
}

/** 유효 정책 — reload 로 저장 후 다시 읽는다. */
export function usePolicy(scope: 'user' | 'team', id: string): [Load<EffectivePolicy>, () => void] {
  const [s, setS] = useState<Load<EffectivePolicy>>({ status: 'loading' });
  const [gen, setGen] = useState(0);
  useEffect(() => {
    let alive = true;
    setS((prev) => (prev.status === 'ready' ? prev : { status: 'loading' }));
    (scope === 'user' ? getEffectivePolicyAction(id) : getTeamEffectivePolicyAction(id)).then((r) => {
      if (alive) setS(r.success ? { status: 'ready', data: r.data } : { status: 'failed' });
    });
    return () => {
      alive = false;
    };
  }, [scope, id, gen]);
  return [s, useCallback(() => setGen((g) => g + 1), [])];
}

const EMPTY_CELL: UserModelCell = {
  input_tokens: 0,
  output_tokens: 0,
  cache_read_tokens: 0,
  cache_write_tokens: 0,
  total_tokens: 0,
  cost_usd: 0,
  requests: 0,
};

export interface TeamUsage {
  byUser: Map<string, UserModelCell>;
  total: UserModelCell;
}

function add(a: UserModelCell, b: UserModelCell): UserModelCell {
  return {
    input_tokens: a.input_tokens + Number(b.input_tokens || 0),
    output_tokens: a.output_tokens + Number(b.output_tokens || 0),
    cache_read_tokens: a.cache_read_tokens + Number(b.cache_read_tokens || 0),
    cache_write_tokens: a.cache_write_tokens + Number(b.cache_write_tokens || 0),
    total_tokens: a.total_tokens + Number(b.total_tokens || 0),
    cost_usd: Number(a.cost_usd) + Number(b.cost_usd || 0),
    requests: a.requests + Number(b.requests || 0),
  };
}

/** 한 번에 받는 팀 구성원 행 수 — admin-api 상한(500)과 같다. 팀 전원이 보통 한 요청. */
export const TEAM_USAGE_PAGE_SIZE = 500;

/**
 * 팀 범위 → 사용자별 합계 + 팀 합계.
 *
 * ⚠️ 예전엔 page_size=50 으로 끝까지 순차 페이지를 넘겼다(300명 팀 = 6 요청, 사용자 상세는
 *    팀 합계 하나 때문에 같은 6 요청). 이제 `totalOnly`(사용자 상세)는 page_size=1 + 서버의
 *    scope_total 한 요청, 구성원 표는 page_size=500 한 요청(500명 초과 팀만 다음 페이지).
 */
export async function fetchTeamUsage(
  teamId: string,
  period: string,
  opts: { signal?: AbortSignal; totalOnly?: boolean } = {},
): Promise<TeamUsage> {
  try {
    return await fetchTeamUsagePaged(teamId, period, opts, opts.totalOnly ? 1 : TEAM_USAGE_PAGE_SIZE);
  } catch (e) {
    // 구 admin-api(page_size ≤ 50)면 422 — 롤링 배포 중에도 구성원 표가 뜨게 예전 크기로 다시.
    if (!opts.totalOnly && e instanceof UserModelFetchError && e.status === 422) {
      return fetchTeamUsagePaged(teamId, period, opts, 50);
    }
    throw e;
  }
}

async function fetchTeamUsagePaged(
  teamId: string,
  period: string,
  opts: { signal?: AbortSignal; totalOnly?: boolean },
  size: number,
): Promise<TeamUsage> {
  const byUser = new Map<string, UserModelCell>();
  let total = { ...EMPTY_CELL };
  let serverTotal: UserModelCell | undefined;
  for (let page = 1; page <= 40; page++) {
    const p = new URLSearchParams({ period, scope: `team:${teamId}`, sort: 'cost', dir: 'desc', page: String(page), page_size: String(size) });
    const res = await fetchUserModelJson<UserModelResponse>(`/api/analytics/user-models?${p}`, opts.signal);
    serverTotal = res.scope_total;
    if (opts.totalOnly && serverTotal) return { byUser, total: { ...EMPTY_CELL, ...serverTotal } };
    // 구 admin-api 는 scope_total 이 없다. page_size=1 로 끝까지 가면 300명 팀이 300요청이므로
    // 예전 크기(50)로 다시 합산한다(롤링 배포 호환 — 첫 1요청은 버리고 재시작).
    if (opts.totalOnly && size === 1) return fetchTeamUsagePaged(teamId, period, opts, 50);
    for (const r of res.rows) {
      byUser.set(r.user_id, r.total);
      total = add(total, r.total);
    }
    if (res.rows.length === 0 || page * res.page_size >= res.total_users) break;
  }
  // 서버 합계가 정본 — 페이지 합과 같은 값이지만 비용 반올림 누적이 없다.
  return { byUser, total: serverTotal ? { ...EMPTY_CELL, ...serverTotal } : total };
}

/**
 * 짧은 공유 캐시 — 사용자 상세와 팀 상세가 같은 팀을 연달아 열거나(트리에서 팀 → 멤버) 같은
 * 화면이 다시 마운트될 때 같은 요청을 다시 보내지 않는다. 진행 중 요청은 공유하고, 구독자가
 * 모두 떠나면 abort 한다(노드를 빨리 넘길 때 버려진 요청이 BFF·admin-api 를 붙잡지 않게).
 * TTL 은 admin-api 결과 캐시(오늘 포함 30 s)와 같다.
 */
const SHARED_TTL_MS = 30_000;
interface SharedEntry<T> {
  at: number;
  promise: Promise<T>;
  ctrl: AbortController;
  refs: number;
  settled: boolean;
}
const sharedCache = new Map<string, SharedEntry<unknown>>();

export function acquireShared<T>(
  key: string,
  fn: (_signal: AbortSignal) => Promise<T>,
): { promise: Promise<T>; release: () => void } {
  const now = Date.now();
  let e = sharedCache.get(key) as SharedEntry<T> | undefined;
  if (e && e.settled && now - e.at > SHARED_TTL_MS) {
    sharedCache.delete(key);
    e = undefined;
  }
  if (!e) {
    const ctrl = new AbortController();
    const entry: SharedEntry<T> = { at: now, promise: fn(ctrl.signal), ctrl, refs: 0, settled: false };
    entry.promise.then(
      () => {
        entry.settled = true;
        entry.at = Date.now();
      },
      () => {
        // 실패·취소는 캐시하지 않는다.
        if (sharedCache.get(key) === entry) sharedCache.delete(key);
      },
    );
    sharedCache.set(key, entry as SharedEntry<unknown>);
    e = entry;
  }
  const entry = e;
  entry.refs += 1;
  let released = false;
  return {
    promise: entry.promise,
    release: () => {
      if (released) return;
      released = true;
      entry.refs -= 1;
      if (entry.refs <= 0 && !entry.settled) {
        entry.ctrl.abort();
        if (sharedCache.get(key) === entry) sharedCache.delete(key);
      }
    },
  };
}

/** 테스트 전용 — 모듈 캐시 비우기. */
export function __resetSharedCache(): void {
  sharedCache.clear();
}

export function useTeamUsage(
  teamId: string | null,
  period: string,
  gen = 0,
  opts: { totalOnly?: boolean } = {},
): Load<TeamUsage> {
  const totalOnly = !!opts.totalOnly;
  const [s, setS] = useState<Load<TeamUsage>>({ status: 'loading' });
  useEffect(() => {
    if (!teamId) {
      setS({ status: 'failed' });
      return;
    }
    let alive = true;
    setS({ status: 'loading' });
    const key = `team-usage|${teamId}|${period}|${gen}|${totalOnly ? 'total' : 'rows'}`;
    const { promise, release } = acquireShared(key, (signal) => fetchTeamUsage(teamId, period, { signal, totalOnly }));
    promise.then(
      (d) => alive && setS({ status: 'ready', data: d }),
      () => alive && setS({ status: 'failed' }),
    );
    return () => {
      alive = false;
      release();
    };
  }, [teamId, period, gen, totalOnly]);
  return s;
}

export function useUserUsage(userId: string, period: string): Load<UserModelDetail> {
  const [s, setS] = useState<Load<UserModelDetail>>({ status: 'loading' });
  useEffect(() => {
    let alive = true;
    const ctrl = new AbortController();
    setS({ status: 'loading' });
    const p = new URLSearchParams({ period, scope: 'all' });
    fetchUserModelJson<UserModelDetail>(`/api/analytics/user-models/${encodeURIComponent(userId)}?${p}`, ctrl.signal).then(
      (d) => alive && setS({ status: 'ready', data: d }),
      // 사용 기록이 없는 사용자는 404 일 수 있다 — 0 으로 보인다(아래 emptyDetail).
      () => alive && setS({ status: 'failed' }),
    );
    return () => {
      alive = false;
      // 노드를 빨리 넘기면 이전 사용자의 상세 요청을 끊는다.
      ctrl.abort();
    };
  }, [userId, period]);
  return s;
}

export function useActivity(ids: string[]): Load<Map<string, UserActivityItem>> {
  const key = [...ids].sort().join(',');
  const [s, setS] = useState<Load<Map<string, UserActivityItem>>>({ status: 'loading' });
  useEffect(() => {
    let alive = true;
    setS({ status: 'loading' });
    const list = key ? key.split(',') : [];
    getUsersActivityAction(list).then((r) => {
      if (!alive) return;
      setS(r.success ? { status: 'ready', data: new Map(r.data.items.map((i) => [i.user_id, i])) } : { status: 'failed' });
    });
    return () => {
      alive = false;
    };
  }, [key]);
  return s;
}

export { EMPTY_CELL };
