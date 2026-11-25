// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 대시보드 COST & BUDGET 카드 4장 + Total (charged) 분해(wave 3).
 *
 * Usage This Month = Total (charged) — 프로바이더가 처리해 청구한 전 상태(성공·실패·취소)의 모델 비용
 * + 웹 검색 비용. 아래 ChargedBreakdown 이 분해를 보여준다.
 *
 * 웹 검색 표시 규칙(배포됐거나 기록 비용 > 0)이 참이면:
 *  - Usage This Month = 모델 + 웹 검색, 부제 "Model $X · Web search $Y (all users)"
 *    (그 기간 웹 검색 비용이 NULL=미추적이면 "Model $X · Web search not tracked (all users)",
 *     아래 'incl. web search' 부제들도 '· web search not tracked')
 *  - Avg Cost Per User · Daily Average(월말 예상 포함) = 모델 + 웹 검색, 부제 "incl. web search"
 *  - Budget Utilization = **모델 비용만**(백엔드 값 그대로), 부제 "Model cost vs budget · web search excluded"
 * 거짓이면 기능 이전과 똑같은 카드(모델 비용·원래 부제).
 * 표시 전용 — 예산·HARD_BLOCK·CPM/CPH 는 계속 모델 비용만 쓴다.
 */

import { BarChart3, CalendarClock, DollarSign, Users } from 'lucide-react';
import { getTranslations } from 'next-intl/server';

import { ChargedBreakdown } from '@/components/common/ChargedBreakdown';
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
  // 웹 검색 비용이 NULL(미추적)이면 합계에 들어간 게 없다 — "$0.00 · incl. web search" 대신 "not tracked".
  const wsTracked = view?.webSearchTracked === true;
  const budgetUtilization = view?.budgetUtilization ?? null;
  // 한도 합계가 0 이면 비율이 정의되지 않는다(백엔드가 null 을 준다) — 경고 등급도 매기지 않는다.
  const alertLevel = budgetUtilization != null ? alertLevelOf(budgetUtilization) : undefined;
  // 앱 필터를 드릴다운 링크로 보존한다(R25-N-UI4).
  const clientSuffix = client !== 'all' ? `&client=${encodeURIComponent(client)}` : '';
  // KPI 카드는 값 하나가 전부라 1센트 미만도 유효숫자 2개($0.0071).
  const usd = (n: number) => fmtUsd(n, { subCent: 'significant' });

  return (
    <div className="flex flex-col gap-4">
    <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4" data-testid="cost-budget-cards">
      <KPICard
        title={t('usageThisMonth')}
        value={view ? usd(view.usageCost) : '—'}
        icon={<DollarSign size={18} aria-hidden="true" />}
        description={
          !view
            ? t('fetchFailed')
            : ws
              ? wsTracked
                ? tw('usageCombinedDesc', { model: usd(view.modelCost), webSearch: usd(view.webSearchCost) })
                : tw('usageModelWsNotTrackedDesc', { model: usd(view.modelCost) })
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
              ? tw(wsTracked ? 'avgCostPerUserCombinedDesc' : 'avgCostPerUserWsNotTrackedDesc', { count: kpi.active_users })
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
              ? tw(wsTracked ? 'dailyAvgProjectionCombined' : 'dailyAvgProjectionWsNotTracked', { amount: usd(view.projection) })
              : t('dailyAvgProjection', { amount: usd(view.projection) })
            : ws
              ? tw(wsTracked ? 'dailyAvgCombinedDesc' : 'dailyAvgWsNotTrackedDesc')
              : t('dailyAvgDesc')
        }
        href={`/analytics?period=${period}${clientSuffix}`}
      />
    </div>
    {/* Total (charged) 분해 — 백엔드가 charged 를 줄 때만(배포 전 백엔드 호환). */}
    {await ChargedBreakdown({ charged: kpi?.charged })}
    </div>
  );
}
