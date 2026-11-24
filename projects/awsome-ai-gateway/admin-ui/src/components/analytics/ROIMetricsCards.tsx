// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { fmtUsd, usdTitle } from '@/lib/utils/format';
import type { WebSearchUsage } from '@/types/entities';
import { WebSearchPriceInfo } from '@/components/common/WebSearchCost';
import { Badge } from '@/components/common/Badge';
import { webSearchNotDeployed, webSearchVisible } from '@/lib/utils/webSearchVisibility';
import type { ReactNode } from 'react';
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
  /** 모니터링 전용 — cost_summary.total_cost_usd(모델 비용)에 포함되지 않는다. */
  web_search?: WebSearchUsage;
}

interface MetricCardProps {
  label: string;
  /** 라벨 옆 부가 요소(ⓘ 툴팁·배지). */
  labelExtra?: ReactNode;
  value: string;
  /** 값 툴팁 — '< $0.01' 로 줄인 금액의 정확한 값. */
  valueTitle?: string;
  description?: string;
  /** 임계 초과 경고 — 보더+설명 텍스트를 warning 색으로. */
  warn?: boolean;
}

function MetricCard({ label, labelExtra, value, valueTitle, description, warn }: MetricCardProps) {
  return (
    <div
      className={`glass glass-hover rounded-apple p-4 flex flex-col gap-2 ${
        warn ? 'border-warning shadow-warning/20' : ''
      }`}
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <p className="text-sm font-medium text-muted-foreground">{label}</p>
        {labelExtra}
      </div>
      <p className="text-2xl font-bold" title={valueTitle}>{value}</p>
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
  const tw = await getTranslations('webSearchCost');
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
      // 경고 시 에러율을 설명 선두에 — 카드엔 별도 배지가 없어 한 줄로 합친다.
      description: err.warn
        ? `${t('errorRateLabel', { rate: err.rate.toFixed(1) })} · ${t('requestsSublineWarn', {
            total: err.total.toLocaleString(),
            errors: err.errors.toLocaleString(),
          })}`
        : t('requestsSubline', {
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
      // KPI 카드는 값 하나가 전부라 1센트 미만도 유효숫자 2개로 보인다($0.0071) —
      // '< $0.01' 은 목록·표·범례 전용(fmtUsd 기본 subCent:'bucket').
      value: fmtUsd(totalCost, { subCent: 'significant' }),
      description: t('totalCostDescription'),
    },
    {
      label: t('averageCostPerUser'),
      value: fmtUsd(avgCostPerUser, { subCent: 'significant' }),
      description: t('activeUsersDescription', { count: summary.active_users }),
    },
  ];

  // 웹 검색 — 모델 비용 카드와 별개(예산 제외). 표시 규칙: 배포됐거나 기록 비용 > 0.
  // null 비용은 "Not tracked" 이지 $0 이 아니다. 무료분·정가 안내는 ⓘ 툴팁.
  const ws = data.web_search;
  const showWs = webSearchVisible(ws);
  if (ws && showWs) {
    const cost = ws.cost_usd;
    // 모델 비용 카드가 웹 검색과 합친 값이 아님을 밝힌다.
    metrics[2].description = tw('modelCost');
    metrics.push({
      label: tw('title'),
      labelExtra: (
        <>
          <WebSearchPriceInfo price={ws.price_per_1k_queries ?? 7} />
          {webSearchNotDeployed(ws) && <Badge tone="amber">{tw('notDeployedBadge')}</Badge>}
        </>
      ),
      value: cost == null ? tw('notTracked') : fmtUsd(cost, { subCent: 'significant' }),
      valueTitle: cost == null ? tw('notTrackedHint') : usdTitle(cost),
      description:
        ws.untracked_queries > 0 && cost != null
          ? `${tw('queries', { count: ws.queries })} · ${tw('untrackedPart', { count: ws.untracked_queries })}`
          : tw('queries', { count: ws.queries }),
    });
  }

  return (
    <div
      className={`grid grid-cols-1 sm:grid-cols-2 gap-4 ${showWs ? 'lg:grid-cols-3 xl:grid-cols-5' : 'lg:grid-cols-4'}`}
    >
      {metrics.map((metric) => (
        <MetricCard key={metric.label} {...metric} />
      ))}
    </div>
  );
}
