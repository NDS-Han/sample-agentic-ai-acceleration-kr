// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import { getTranslations } from 'next-intl/server';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { fetchMyBudget, fetchMyPeriods, fetchMyUsage } from '@/lib/actions/my';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';
import { MyUsageDashboard } from '@/components/my/MyUsageDashboard';
import { PeriodSelector } from '@/components/dashboard/PeriodSelector';
import { ErrorState } from '@/components/common/ErrorState';
import { currentCalendarMonth, isMonth } from '@/lib/utils/period';

async function BudgetSection() {
  const data = await fetchMyBudget().catch(() => null);
  return data ? <MyBudgetCard data={data} /> : <ErrorState compact />;
}

async function UsageSection({ period }: { period?: string }) {
  const data = await fetchMyUsage(period).catch(() => null);
  return data ? <MyUsageDashboard data={data} /> : <ErrorState />;
}

interface MyPageProps {
  searchParams: { period?: string };
}

export default async function MyPage({ searchParams }: MyPageProps) {
  const t = await getTranslations('my');

  // ?period 지원은 있었지만 선택 UI 가 없었다 — 본인 월 목록 + 월 선택기를 둔다.
  // /admin/dashboard/periods 는 admin 전용이라 DEVELOPER/TEAM_LEADER 는 본인
  // 스코프의 /admin/my/periods 를 쓴다. 실패 시 이번 달만 보이게 한다.
  const periods = await fetchMyPeriods().catch(() => [] as string[]);
  const requested = searchParams.period;
  const effectiveMonth = isMonth(requested)
    ? requested
    : (periods[0] ?? currentCalendarMonth());
  // 이번 달이 목록에 없어도 버튼은 항상 보이게(신규 사용자·월초 빈 상태).
  const selectorPeriods = periods.includes(currentCalendarMonth())
    ? periods
    : [currentCalendarMonth(), ...periods];

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between flex-wrap gap-3">
        <h1 className="text-2xl font-bold">{t('pageTitle')}</h1>
        <PeriodSelector periods={selectorPeriods} current={effectiveMonth} />
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          <Suspense fallback={<SkeletonCard count={1} />}>
            <BudgetSection />
          </Suspense>
        </div>
        <div className="lg:col-span-2">
          <Suspense key={effectiveMonth} fallback={<SkeletonCard count={3} />}>
            <UsageSection period={effectiveMonth} />
          </Suspense>
        </div>
      </div>
    </div>
  );
}
