// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import type { AnalyticsFilterForm } from '@/types/api';
import { adminAPI } from '@/lib/api-client';
import { buildAnalyticsQuery } from '@/lib/utils/analyticsQuery';
import { LazyTokenMixDonut } from './LazyCharts';
import { ErrorState } from '@/components/common/ErrorState';
import type { TokenBreakdownData } from './TokenMixDonutClient';

interface TokenAnalysisCardProps {
  filter: AnalyticsFilterForm;
  latestMonth?: string;
}

interface AnalyticsAPIResponse {
  token_breakdown?: TokenBreakdownData;
}

export async function TokenAnalysisCard({ filter, latestMonth }: TokenAnalysisCardProps) {
  const t = await getTranslations('analytics');
  // ROIMetricsCards 등과 **동일** buildAnalyticsQuery → 요청 메모이제이션으로 dedup.
  const data = await adminAPI
    .get<AnalyticsAPIResponse>('/admin/analytics', buildAnalyticsQuery(filter, latestMonth))
    .catch(() => null);

  const tb = data?.token_breakdown;
  return (
    <div className="glass glass-hover rounded-apple p-4">
      <h3 className="text-sm font-semibold mb-3">{t('tokenAnalysis')}</h3>
      {data === null ? (
        // 조회 실패와 "토큰 데이터 0"을 구분 — 실패는 재시도 가능 상태.
        <ErrorState compact />
      ) : tb && tb.total_tokens > 0 ? (
        <LazyTokenMixDonut data={tb} />
      ) : (
        <div className="flex items-center justify-center h-64 text-sm text-muted-foreground">
          {t('noData')}
        </div>
      )}
    </div>
  );
}
