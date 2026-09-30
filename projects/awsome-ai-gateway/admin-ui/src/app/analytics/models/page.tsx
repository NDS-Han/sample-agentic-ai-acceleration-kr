// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import Link from 'next/link';
import { getTranslations } from 'next-intl/server';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { fetchModelCostAnalytics } from '@/lib/actions/analytics-models';
import { fetchAvailablePeriods } from '@/lib/actions/dashboard';
import { ModelCostDetail } from '@/components/analytics/ModelCostDetail';
import { PeriodSelector } from '@/components/dashboard/PeriodSelector';
import { ErrorState } from '@/components/common/ErrorState';
import { isMonth } from '@/lib/utils/period';

interface ModelCostPageProps {
  searchParams: { period?: string };
}

async function ModelCostSection({ period }: { period?: string }) {
  const data = await fetchModelCostAnalytics(period).catch(() => null);
  return data ? <ModelCostDetail data={data} /> : <ErrorState />;
}

export default async function ModelCostPage({ searchParams }: ModelCostPageProps) {
  const t = await getTranslations('analyticsModels');

  // 대시보드·/analytics 와 같은 월 목록 소스 — ?period 가 데이터 있는 월이면 존중.
  const { periods, latest } = await fetchAvailablePeriods();
  const requested = searchParams.period;
  const effectiveMonth = isMonth(requested) && periods.includes(requested) ? requested : latest;

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <div>
          <Link
            href="/analytics"
            className="text-sm text-muted-foreground hover:text-foreground transition-colors"
          >
            ← {t('backToAnalytics')}
          </Link>
          <h1 className="text-2xl font-bold mt-1">{t('pageTitle')}</h1>
        </div>
        <PeriodSelector periods={periods} current={effectiveMonth} />
      </div>

      <Suspense key={effectiveMonth} fallback={<SkeletonCard count={3} />}>
        <ModelCostSection period={effectiveMonth} />
      </Suspense>
    </div>
  );
}
