// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';
import { buildAnalyticsQuery, fetchAnalyticsOnce } from '@/lib/utils/analyticsQuery';
import { CostTrendCard } from '@/components/dashboard/CostTrendCard';
import { monthRange, type TrendRange } from '@/lib/utils/trendSeries';
import { ErrorState } from '@/components/common/ErrorState';

interface CostTrendChartProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface AnalyticsAPIResponse {
  trends: { date: string; cost_usd: number; requests: number }[];
  trends_by_team?: {
    team: string;
    team_id: string;
    dept_name?: string | null;
    points: { date: string; cost_usd: number; requests: number }[];
  }[];
}

export async function CostTrendChart({ filter, latestMonth }: CostTrendChartProps) {
  const query = buildAnalyticsQuery(filter, latestMonth);
  const data = await fetchAnalyticsOnce<AnalyticsAPIResponse>(JSON.stringify(query)).catch(
    () => null,
  );
  // 조회 기간 — 직접 입력 구간이면 그 구간, 아니면 집계 월 전체.
  const range: TrendRange | undefined =
    typeof query.start_date === 'string' && typeof query.end_date === 'string'
      ? { start: query.start_date, end: query.end_date }
      : monthRange(typeof query.period === 'string' ? query.period : undefined);

  const trends = (data?.trends ?? []).map((t) => ({
    date: t.date,
    cost_usd: Number(t.cost_usd),
    requests: Number(t.requests ?? 0),
  }));
  const trendsByTeam = (data?.trends_by_team ?? []).map((tt) => ({
    team: tt.team,
    team_id: tt.team_id,
    dept_name: tt.dept_name ?? null,
    points: (tt.points ?? []).map((p) => ({
      date: p.date,
      cost_usd: Number(p.cost_usd),
      requests: Number(p.requests ?? 0),
    })),
  }));

  // 대시보드와 같은 recharts 카드를 재사용한다 — 과거엔 여기만 Chart.js 로 따로
  // 그려 색·범례·툴팁이 미묘하게 어긋났다(같은 /admin/analytics 응답인데 다른
  // 렌더러). 시리즈 조립(buildTrendSeries)는 원래 단일 소스였으므로 렌더러만 통합.
  return data ? (
    <CostTrendCard trends={trends} trendsByTeam={trendsByTeam} range={range} />
  ) : (
    <div className="glass glass-hover rounded-apple p-4">
      <ErrorState compact />
    </div>
  );
}
