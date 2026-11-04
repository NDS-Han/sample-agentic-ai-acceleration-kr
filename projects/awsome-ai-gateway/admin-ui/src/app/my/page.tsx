// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import { getTranslations } from 'next-intl/server';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { fetchMyBudget, fetchMyPeriods, fetchMyUsage } from '@/lib/actions/my';
import { MyUsageDashboard } from '@/components/my/MyUsageDashboard';
import { PeriodSelector } from '@/components/dashboard/PeriodSelector';
import { ErrorState } from '@/components/common/ErrorState';
import { currentCalendarMonth, isMonth } from '@/lib/utils/period';

async function UsageSection({ period }: { period?: string }) {
  // 예산과 사용량을 병렬로 fetch — 직렬이면 사용량이 끝난 뒤에야 예산 요청이
  // 시작되는 워터폴이 생긴다. 예산 실패만 카드 영역으로 격리한다.
  const [usage, budget] = await Promise.all([
    fetchMyUsage(period).catch(() => null),
    fetchMyBudget().catch(() => null),
  ]);
  return usage ? (
    <MyUsageDashboard data={usage} budget={budget} />
  ) : (
    <ErrorState />
  );
}

interface MyPageProps {
  searchParams: Promise<{ period?: string }>;
}

export default async function MyPage(props: MyPageProps) {
  const searchParams = await props.searchParams;
  const t = await getTranslations('my');

  // ?period 지원은 있었지만 선택 UI 가 없었다 — 본인 월 목록 + 월 선택기를 둔다.
  // /admin/dashboard/periods 는 admin 전용이라 DEVELOPER/TEAM_LEADER 는 본인
  // 스코프의 /admin/my/periods 를 쓴다. 실패 시 이번 달만 보이게 한다.
  const periods = await fetchMyPeriods().catch(() => [] as string[]);
  const requested = searchParams.period;
  // 기본은 이번 달 — 옆의 예산 카드가 항상 이번 달 기준이라, 데이터 있는 최신 월로
  // 열리면 두 카드의 기준이 어긋난다. 이번 달 사용이 없으면 빈 상태를 보인다.
  const effectiveMonth = isMonth(requested)
    ? requested
    : currentCalendarMonth();
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

      <Suspense key={effectiveMonth} fallback={<SkeletonCard count={4} />}>
        <UsageSection period={effectiveMonth} />
      </Suspense>
    </div>
  );
}
