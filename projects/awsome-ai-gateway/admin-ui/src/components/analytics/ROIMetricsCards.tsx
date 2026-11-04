// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { fmtUsd } from '@/lib/utils/format';
import { errorRateInfo } from '@/lib/utils/errorRate';
import type { AnalyticsFilterForm } from '@/types/api';
import { buildAnalyticsQuery, fetchAnalyticsOnce } from '@/lib/utils/analyticsQuery';
import { ErrorState } from '@/components/common/ErrorState';

interface ROIMetricsCardsProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface AnalyticsAPIResponse {
  period: string;
  currency: string;
  cost_summary: {
    total_requests: number;
    error_requests: number;
    total_tokens: number;
    total_cost_usd: number;
    active_users: number;
    avg_cost_per_user_usd: number;
  };
  by_model: { model: string; requests: number; cost_usd: number }[];
  by_team: { team: string; team_id: string; cost_usd: number; active_users: number }[];
  trends: { date: string; cost_usd: number; requests: number }[];
}

interface MetricCardProps {
  label: string;
  value: string;
  description?: string;
  /** 임계 초과 경고 — 보더+설명 텍스트를 warning 색으로. */
  warn?: boolean;
}

function MetricCard({ label, value, description, warn }: MetricCardProps) {
  return (
    <div
      className={`glass glass-hover rounded-apple p-4 flex flex-col gap-2 ${
        warn ? 'border-warning shadow-warning/20' : ''
      }`}
    >
      <p className="text-sm font-medium text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold">{value}</p>
      {description && (
        <p className={`text-xs ${warn ? 'text-warning font-medium' : 'text-muted-foreground'}`}>
          {description}
        </p>
      )}
    </div>
  );
}

export async function ROIMetricsCards({ filter, latestMonth }: ROIMetricsCardsProps) {
  const t = await getTranslations('analytics');
  const data = await fetchAnalyticsOnce<AnalyticsAPIResponse>(
    JSON.stringify(buildAnalyticsQuery(filter, latestMonth)),
  ).catch(() => null);

  if (!data?.cost_summary) {
    // 조회 실패(네트워크/5xx) — 재시도 가능한 에러 상태로 구분한다.
    return <ErrorState compact />;
  }

  const summary = data.cost_summary;
  const totalCost = Number(summary.total_cost_usd ?? 0);
  const avgCostPerUser = Number(summary.avg_cost_per_user_usd ?? 0);
  // 성공 카드와 같은 모집단 — 서브라인으로 전체·에러율을 얹는다(대시보드와 동일 임계값).
  const err = errorRateInfo(summary.total_requests, summary.error_requests ?? 0);

  const metrics: MetricCardProps[] = [
    {
      label: t('totalRequests'),
      value: summary.total_requests.toLocaleString(),
      description: t('requestsSubline', {
        total: err.total.toLocaleString(),
        errors: err.errors.toLocaleString(),
        rate: err.rate.toFixed(1),
      }),
      warn: err.warn,
    },
    {
      label: t('totalTokens'),
      value: summary.total_tokens.toLocaleString(),
      description: t('totalTokensDescription'),
    },
    {
      label: t('totalCost'),
      value: fmtUsd(totalCost),
      description: t('totalCostDescription'),
    },
    {
      label: t('averageCostPerUser'),
      value: fmtUsd(avgCostPerUser),
      description: t('activeUsersDescription', { count: summary.active_users }),
    },
  ];

  return (
    <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-4">
      {metrics.map((metric) => (
        <MetricCard key={metric.label} {...metric} />
      ))}
    </div>
  );
}
