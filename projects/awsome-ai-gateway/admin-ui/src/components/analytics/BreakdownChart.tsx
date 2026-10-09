// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import type { AnalyticsFilterForm } from '@/types/api';
import { adminAPI } from '@/lib/api-client';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';
import { LazyBreakdownChart } from './LazyCharts';
import { ErrorState } from '@/components/common/ErrorState';
import { teamDisplayName } from '@/lib/utils/trendSeries';
import { modelDisplay } from '@/lib/utils/modelLabel';

interface BreakdownChartProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface AnalyticsAPIResponse {
  by_model: { model: string; requests: number; cost_usd: number; display_name?: string | null }[];
  by_team: {
    team: string;
    team_id: string;
    cost_usd: number;
    active_users: number;
    dept_name?: string | null;
  }[];
  by_user: { user: string; email: string; cost_usd: number; requests: number }[];
}

export async function BreakdownChart({ filter, latestMonth }: BreakdownChartProps) {
  const t = await getTranslations('analytics');
  const data = await adminAPI
    .get<AnalyticsAPIResponse>('/admin/analytics', buildAnalyticsQuery(filter, latestMonth))
    .catch(() => null);

  let labels: string[] = [];
  let values: number[] = [];
  let title = t('totalCost');

  if (data) {
    if (filter.group_by === 'team') {
      // canonical 규칙과 동일: 같은 화면의 추이 범례(teamDisplayName)와 라벨이
      // 갈라지면 동명 팀(NDS_Developers vs SSIR_Developers)을 구분할 수 없다.
      labels = (data.by_team ?? []).map((b) =>
        teamDisplayName({ team: b.team, dept_name: b.dept_name ?? null })
      );
      values = (data.by_team ?? []).map((b) => Number(b.cost_usd));
      title = t('costByTeam');
    } else if (filter.group_by === 'user') {
      // §60.9: 백엔드 by_user 집계 사용(이전엔 by_model 로 잘못 표시됐음).
      labels = (data.by_user ?? []).map((b) => b.user || b.email);
      values = (data.by_user ?? []).map((b) => Number(b.cost_usd));
      title = t('costByUser');
    } else {
      // 대시보드 도넛과 같은 표기 규칙 — 카탈로그 display_name 우선, 없으면 alias.
      labels = (data.by_model ?? []).map((b) => modelDisplay(b.model, b.display_name));
      values = (data.by_model ?? []).map((b) => Number(b.cost_usd));
      title = t('costByModel');
    }
  }

  return (
    <div className="glass glass-hover rounded-apple p-4">
      {data ? (
        <LazyBreakdownChart labels={labels} values={values} title={title} />
      ) : (
        <ErrorState compact />
      )}
    </div>
  );
}
