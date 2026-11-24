// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 대시보드 COST & BUDGET 카드 4장.
 *
 * 웹 검색 표시 규칙(배포됐거나 기록 비용 > 0)이 참이면:
 *  - Usage This Month = 모델 + 웹 검색, 부제 "Model $X · Web search $Y (all users)"
 *  - Avg Cost Per User · Daily Average(월말 예상 포함) = 모델 + 웹 검색, 부제 "incl. web search"
 *  - Budget Utilization = **모델 비용만**(백엔드 값 그대로), 부제 "Model cost vs budget · web search excluded"
 * 거짓이면 기능 이전과 똑같은 카드(모델 비용·원래 부제).
 * 표시 전용 — 예산·HARD_BLOCK·CPM/CPH 는 계속 모델 비용만 쓴다.
 */

import { BarChart3, CalendarClock, DollarSign, Users } from 'lucide-react';
import { getTranslations } from 'next-intl/server';

import { KPICard } from '@/components/common/KPICard';
import type { DashboardKPI } from '@/lib/actions/dashboard';
import { alertLevelOf } from '@/lib/utils/alertLevel';
import { costBudgetView } from '@/lib/utils/dashboardCost';
import { fmtUsd } from '@/lib/utils/format';

export async function CostBudgetCards({
  kpi,
  period,
  client,
  now,
}: {
  kpi: DashboardKPI | null;
  period: string;
  client: string;
  /** 테스트용 리포팅 TZ "오늘". */
  now?: { y: number; m: number; d: number };
}) {
  const t = await getTranslations('dashboard');
  const tb = await getTranslations('budgets');
  const tw = await getTranslations('webSearchCost');

  const view = kpi ? costBudgetView(kpi, period, now) : null;
  const ws = view?.showWebSearch === true;
  const budgetUtilization = view?.budgetUtilization ?? null;
  // 한도 합계가 0 이면 비율이 정의되지 않는다(백엔드가 null 을 준다) — 경고 등급도 매기지 않는다.
  const alertLevel = budgetUtilization != null ? alertLevelOf(budgetUtilization) : undefined;
  // 앱 필터를 드릴다운 링크로 보존한다(R25-N-UI4).
  const clientSuffix = client !== 'all' ? `&client=${encodeURIComponent(client)}` : '';
  // KPI 카드는 값 하나가 전부라 1센트 미만도 유효숫자 2개($0.0071).
  const usd = (n: number) => fmtUsd(n, { subCent: 'significant' });

  return (
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4" data-testid="cost-budget-cards">
      <KPICard
        title={t('usageThisMonth')}
        value={view ? usd(view.usageCost) : '—'}
        icon={<DollarSign size={18} aria-hidden="true" />}
        description={
          !view
            ? t('fetchFailed')
            : ws
              ? tw('usageCombinedDesc', { model: usd(view.modelCost), webSearch: usd(view.webSearchCost) })
              : t('usageThisMonthDesc')
        }
        href={`/analytics?period=${period}${clientSuffix}`}
      />
      <KPICard
        title={t('budgetUtilization')}
        value={budgetUtilization != null ? `${budgetUtilization.toFixed(1)}%` : '—'}
        icon={<BarChart3 size={18} aria-hidden="true" />}
        alertLevel={alertLevel}
        alertLabel={alertLevel ? tb(`alertLevels.${alertLevel}`) : undefined}
        // R25-N-UI3 — kpi 는 왔는데 utilization 만 null 이면 "한도 미설정"(정상 상태).
        description={
          !kpi
            ? t('fetchFailed')
            : budgetUtilization != null
              ? ws
                ? tw('budgetUtilizationCombinedDesc')
                : t('budgetUtilizationDesc')
              : t('budgetNotConfigured')
        }
        href="/budgets"
      />
      <KPICard
        title={t('avgCostPerUser')}
        value={view ? usd(view.avgPerUser) : '—'}
        icon={<Users size={18} aria-hidden="true" />}
        description={
          !kpi
            ? t('fetchFailed')
            : ws
              ? tw('avgCostPerUserCombinedDesc', { count: kpi.active_users })
              : t('avgCostPerUserDesc', { count: kpi.active_users })
        }
        href={`/analytics?period=${period}&group_by=user${clientSuffix}`}
      />
      <KPICard
        title={t('dailyAvg')}
        value={view ? usd(view.dailyAvg) : '—'}
        icon={<CalendarClock size={18} aria-hidden="true" />}
        description={
          view?.projection != null
            ? ws
              ? tw('dailyAvgProjectionCombined', { amount: usd(view.projection) })
              : t('dailyAvgProjection', { amount: usd(view.projection) })
            : ws
              ? tw('dailyAvgCombinedDesc')
              : t('dailyAvgDesc')
        }
        href={`/analytics?period=${period}${clientSuffix}`}
      />
    </div>
  );
}
