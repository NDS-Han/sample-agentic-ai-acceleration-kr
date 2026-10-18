// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cache } from 'react';
import type { AnalyticsFilterForm } from '@/types/api';
import { resolveMonth } from '@/lib/utils/period';
import { adminAPI } from '@/lib/api-client';

/**
 * `/admin/analytics` 질의 파라미터 빌더 — **단일 출처**.
 *
 * analytics 페이지의 여러 서버 컴포넌트(ROIMetricsCards/CostTrendChart/BreakdownChart)
 * 와 퀵챗 컨텍스트 등록(ContextSection)이 모두 같은 엔드포인트를 같은 파라미터로
 * 호출한다. Next.js **요청 메모이제이션**은 동일 렌더 내 동일 URL+옵션 fetch 를
 * 1회로 dedup 하므로, 모든 호출처가 이 함수를 거쳐 **바이트 동일 요청**을 만들어야
 * 중복 네트워크 호출이 발생하지 않는다(파라미터가 갈리면 dedup 이 조용히 깨짐).
 */
export function buildAnalyticsQuery(
  filter: AnalyticsFilterForm,
  latestMonth?: string,
): Record<string, string | number | undefined> {
  const query: Record<string, string | number | undefined> = {
    period: resolveMonth(filter, latestMonth),
    group_by: filter.group_by,
    scope: filter.scope ?? 'all',
  };
  // custom 직접 입력 — 두 날짜가 다 있을 때만 구간을 실어 보낸다. 백엔드는 둘이
  // 있으면 period 대신 일자 구간으로 집계한다. start 만 있는 진행 중 상태는
  // 그 날짜의 월 집계로 미리보기(resolveMonth 가 start 의 월을 뽑는다).
  if (filter.period === 'custom' && filter.start_date && filter.end_date) {
    query.start_date = filter.start_date;
    query.end_date = filter.end_date;
  }
  // 앱 필터 — 'all'/미지정이면 파라미터를 생략해 백엔드 기본(전체)과 바이트
  // 동일 요청을 유지한다(dedup 키 일치).
  if (filter.client && filter.client !== 'all') {
    query.client = filter.client;
  }
  return query;
}

/**
 * `/admin/analytics` 렌더-패스 dedup + wire 정규화 — **단일 진입점**.
 *
 * adminAPI 는 항상 `signal` 을 붙이므로 Next 의 fetch 메모이제이션이 꺼진다 —
 * 같은 페이지의 5개 서버 컴포넌트가 바이트 동일 요청을 5번 보낸다(R25-N-UI1).
 * React `cache()` 는 렌더 패스 안에서 인자 기준으로 메모이즈하므로, 호출처는
 * `JSON.stringify(buildAnalyticsQuery(...))` 라는 문자열 키를 넘긴다.
 *
 * 동시에 백엔드 Decimal→JSON 문자열("1.234567")을 여기서 숫자로 정규화한다.
 * UI 타입은 number 를 주장하는데 wire 는 string 이라, Number() 로 안 감싼
 * 소비처(포맷터·비교)가 조용히 깨진다(R25-N9/S8 같은 계열).
 */
export const fetchAnalyticsOnce = cache(
  async <T = unknown>(queryJson: string): Promise<T> => {
    const data = await adminAPI.get<T>('/admin/analytics', JSON.parse(queryJson));
    return normalizeAnalyticsNumbers(data);
  },
);

function num(v: unknown): unknown {
  if (v == null) return v;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : v;
}

/** 알려진 금액/비율 필드만 숫자로 강제한다 — 모르는 키는 그대로 통과. */
function normalizeAnalyticsNumbers<T>(data: T): T {
  if (!data || typeof data !== 'object') return data;
  const d = data as Record<string, unknown>;
  const s = d.cost_summary as Record<string, unknown> | undefined;
  if (s) {
    s.total_cost_usd = num(s.total_cost_usd);
    s.avg_cost_per_user_usd = num(s.avg_cost_per_user_usd);
  }
  for (const list of ['by_team', 'by_user', 'by_model'] as const) {
    for (const row of (d[list] as Record<string, unknown>[] | undefined) ?? []) {
      row.cost_usd = num(row.cost_usd);
    }
  }
  for (const t of (d.trends as Record<string, unknown>[] | undefined) ?? []) {
    t.cost_usd = num(t.cost_usd);
  }
  for (const tt of (d.trends_by_team as Record<string, unknown>[] | undefined) ?? []) {
    for (const p of (tt.points as Record<string, unknown>[] | undefined) ?? []) {
      p.cost_usd = num(p.cost_usd);
    }
  }
  return data;
}
