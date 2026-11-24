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

/** 팀 범위 전체 행(페이지를 끝까지) → 사용자별 합계 + 팀 합계. */
export async function fetchTeamUsage(teamId: string, period: string): Promise<TeamUsage> {
  const byUser = new Map<string, UserModelCell>();
  let total = { ...EMPTY_CELL };
  for (let page = 1; page <= 40; page++) {
    const p = new URLSearchParams({ period, scope: `team:${teamId}`, sort: 'cost', dir: 'desc', page: String(page), page_size: '50' });
    const res = await fetchUserModelJson<UserModelResponse>(`/api/analytics/user-models?${p}`);
    for (const r of res.rows) {
      byUser.set(r.user_id, r.total);
      total = add(total, r.total);
    }
    if (res.rows.length === 0 || page * res.page_size >= res.total_users) break;
  }
  return { byUser, total };
}

export function useTeamUsage(teamId: string | null, period: string, gen = 0): Load<TeamUsage> {
  const [s, setS] = useState<Load<TeamUsage>>({ status: 'loading' });
  useEffect(() => {
    if (!teamId) {
      setS({ status: 'failed' });
      return;
    }
    let alive = true;
    setS({ status: 'loading' });
    fetchTeamUsage(teamId, period).then(
      (d) => alive && setS({ status: 'ready', data: d }),
      () => alive && setS({ status: 'failed' }),
    );
    return () => {
      alive = false;
    };
  }, [teamId, period, gen]);
  return s;
}

export function useUserUsage(userId: string, period: string): Load<UserModelDetail> {
  const [s, setS] = useState<Load<UserModelDetail>>({ status: 'loading' });
  useEffect(() => {
    let alive = true;
    setS({ status: 'loading' });
    const p = new URLSearchParams({ period, scope: 'all' });
    fetchUserModelJson<UserModelDetail>(`/api/analytics/user-models/${encodeURIComponent(userId)}?${p}`).then(
      (d) => alive && setS({ status: 'ready', data: d }),
      // 사용 기록이 없는 사용자는 404 일 수 있다 — 0 으로 보인다(아래 emptyDetail).
      () => alive && setS({ status: 'failed' }),
    );
    return () => {
      alive = false;
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
