// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import { KPICard } from '@/components/common/KPICard';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { AlertLevel } from '@/types/enums';
import {
  DollarSign,
  Key,
  Cpu,
  BarChart3,
  Activity,
  Coins,
  Users,
  CalendarClock,
} from 'lucide-react';
import { Suspense } from 'react';
import { getTranslations } from 'next-intl/server';
import {
  fetchDashboardSummary,
  fetchModelShare,
  fetchTeamOptions,
  fetchAnalytics,
  fetchBudgetSummary,
  fetchTopUsers,
  fetchTopTeams,
  fetchAvailablePeriods,
  fetchClientShare,
  type BudgetSummaryItem,
  type ClientShareResponse,
} from '@/lib/actions/dashboard';
import { ModelShareDonutClient } from '@/components/dashboard/ModelShareDonutClient';
import { ClientShareDonutClient } from '@/components/dashboard/ClientShareDonutClient';
import { CostTrendCard } from '@/components/dashboard/CostTrendCard';
import { TopSpendTable, type TopSpendRow } from '@/components/dashboard/TopSpendTable';
import { PeriodSelector } from '@/components/dashboard/PeriodSelector';
import { ClientFilter } from '@/components/dashboard/ClientFilter';
import { kstNowParts } from '@/lib/utils/period';

interface BudgetSummaryResponse {
  summary: BudgetSummaryItem[];
}

interface KeyCountResponse {
  count: number;
}

function calcAlertLevel(utilization: number): (typeof AlertLevel)[keyof typeof AlertLevel] {
  if (utilization >= 95) return AlertLevel.CRITICAL;
  if (utilization >= 80) return AlertLevel.WARNING;
  return AlertLevel.NORMAL;
}

function formatTokens(n: number): string {
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(2)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}K`;
  return n.toLocaleString();
}

function fmtUsd2(n: number): string {
  return `$${n.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/**
 * 일 평균 소비 + (당월이면) 월말 예상.
 * 실데이터: dashboard summary 의 total_cost_usd 를 경과일로 나눔.
 * - 당월: 오늘까지 경과일로 나눠 일평균 → 그 달 총일수 곱해 월말 예상(선형 추정).
 * - 과거월: 그 달 총일수로 나눔(예상 없음, 이미 확정).
 */
function computeDailyAvg(period: string, totalCost: number): {
  dailyAvg: number;
  projection: number | null;
} {
  const [y, m] = period.split('-').map(Number);
  const daysInMonth = new Date(y, m, 0).getDate();
  // ⚠️ "지금" 은 KST 로 구한다. 분자(summary.total_cost_usd)는 백엔드에서 KST 버킷으로
  //    집계되는데 분모를 pod 의 UTC 시계로 나누면 매일 00:00~09:00 KST 사이에 경과일이
  //    하루 적어 일평균이 과대계상되고, 매월 1일 그 9시간 동안은 isCurrentMonth 가
  //    false 가 되어 월말 예상이 아무 설명 없이 사라진다.
  const kstNow = kstNowParts();
  const isCurrentMonth = y === kstNow.y && m === kstNow.m;
  const elapsedDays = isCurrentMonth ? kstNow.d : daysInMonth;
  const dailyAvg = elapsedDays > 0 ? totalCost / elapsedDays : 0;
  const projection = isCurrentMonth ? dailyAvg * daysInMonth : null;
  return { dailyAvg, projection };
}

async function DashboardKPIs({ period, client }: { period: string; client: string }) {
  const t = await getTranslations('dashboard');
  const [budgetResult, keysResult, modelsResult, summaryResult] = await Promise.allSettled([
    adminAPI.get<BudgetSummaryResponse>('/admin/budgets/summary', { period }),
    adminAPI.get<KeyCountResponse>('/admin/keys/count', { status: 'ACTIVE' }),
    // ⚠️ ModelListItem(표시용 타입, is_active 보유) 로 캐스팅하면 안 된다. /admin/models 의
    //    실제 응답은 `status: 'ACTIVE' | 'INACTIVE'` 이고 is_active 를 내보낸 적이 없다.
    //    다른 3개 소비처(models/page.tsx, budgets/page.tsx, lib/actions/models.ts)는
    //    status → is_active 매퍼를 거치는데 이 화면만 생짜 캐스팅이라, 필터가 전부
    //    undefined 를 만나 "활성 모델 수" 가 영구히 0 이었다(TS 에러도 콘솔 경고도 없음).
    adminAPI.get<{ items: Array<{ status: string }> }>('/admin/models'),
    fetchDashboardSummary(period, client),
  ]);

  const budgetData = budgetResult.status === 'fulfilled' ? budgetResult.value : null;
  const keysData = keysResult.status === 'fulfilled' ? keysResult.value : null;
  const modelsData = modelsResult.status === 'fulfilled' ? modelsResult.value : null;
  const summary = summaryResult.status === 'fulfilled' ? summaryResult.value : null;

  // 이번 달 사용량/예산: TEAM 행 + 팀 미소속 USER 행을 합산.
  const summaryItems = budgetData?.summary ?? [];
  const teamItems = summaryItems.filter((i) => i.target_type === 'team');
  const teamlessUsers = summaryItems.filter((i) => i.target_type === 'user' && !i.team_id);
  const aggregateItems = [...teamItems, ...teamlessUsers];
  const totalUsageUsd = aggregateItems.reduce((sum, i) => sum + parseFloat(i.used_usd || '0'), 0);
  const totalLimitUsd = aggregateItems.reduce(
    (sum, i) => sum + (i.limit_usd != null ? parseFloat(i.limit_usd) : 0),
    0,
  );
  const budgetUtilization = totalLimitUsd > 0 ? (totalUsageUsd / totalLimitUsd) * 100 : 0;
  // ⚠️ 실패(403/네트워크)를 0 으로 접지 말 것. TEAM_LEADER 는 /admin/keys/count 에서
  //    403 을 받는데 예전 `?? 0` 은 그걸 "활성 키 0개" 라는 **사실 진술**로 렌더했다.
  //    다른 카드들과 같은 '—' + fetchFailed 관례를 따른다.
  const activeKeys = keysData ? keysData.count : null;
  const activeModels = modelsData
    ? (modelsData.items ?? []).filter((m) => m.status === 'ACTIVE').length
    : null;
  const alertLevel = calcAlertLevel(budgetUtilization);

  // 일 평균 / 월말 예상 — summary 의 total_cost_usd 기반 (가짜 없음, 파생값)
  const { dailyAvg, projection } = summary
    ? computeDailyAvg(period, summary.total_cost_usd)
    : { dailyAvg: 0, projection: null };

  return (
    <div className="space-y-6">
      {/* ── 비용 & 예산 ── */}
      <section className="space-y-3">
        <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
          {t('costAndBudget')}
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <KPICard
            title={t('usageThisMonth')}
            value={fmtUsd2(summary?.total_cost_usd ?? totalUsageUsd)}
            icon={<DollarSign size={18} aria-hidden="true" />}
            description={t('usageThisMonthDesc')}
          />
          <KPICard
            title={t('budgetUtilization')}
            value={`${budgetUtilization.toFixed(1)}%`}
            icon={<BarChart3 size={18} aria-hidden="true" />}
            alertLevel={alertLevel}
            description={t('budgetUtilizationDesc')}
          />
          <KPICard
            title={t('avgCostPerUser')}
            value={summary ? fmtUsd2(summary.cost_per_user_usd) : '—'}
            icon={<Users size={18} aria-hidden="true" />}
            description={summary ? t('avgCostPerUserDesc', { count: summary.active_users }) : t('fetchFailed')}
          />
          <KPICard
            title={t('dailyAvg')}
            value={summary ? fmtUsd2(dailyAvg) : '—'}
            icon={<CalendarClock size={18} aria-hidden="true" />}
            description={
              projection != null
                ? t('dailyAvgProjection', { amount: fmtUsd2(projection) })
                : t('dailyAvgDesc')
            }
          />
        </div>
      </section>

      {/* ── 사용량 & 시스템 ── */}
      <section className="space-y-3">
        <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
          {t('usageAndSystem')}
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          <KPICard
            title={t('totalRequests')}
            value={summary ? summary.total_requests.toLocaleString() : '—'}
            icon={<Activity size={18} aria-hidden="true" />}
            description={t('totalRequestsDesc')}
          />
          <KPICard
            title={t('totalTokens')}
            value={summary ? formatTokens(summary.total_tokens) : '—'}
            icon={<Coins size={18} aria-hidden="true" />}
            description={t('totalTokensDesc')}
          />
          <KPICard
            title={t('activeKeys')}
            value={activeKeys != null ? activeKeys.toLocaleString() : '—'}
            icon={<Key size={18} aria-hidden="true" />}
            description={activeKeys != null ? t('activeKeysDesc') : t('fetchFailed')}
          />
          <KPICard
            title={t('activeModels')}
            value={activeModels != null ? activeModels.toLocaleString() : '—'}
            icon={<Cpu size={18} aria-hidden="true" />}
            description={activeModels != null ? t('activeModelsDesc') : t('fetchFailed')}
          />
        </div>
      </section>
    </div>
  );
}

async function TrendAndDistribution({ period, client }: { period: string; client: string }) {
  const t = await getTranslations('dashboard');
  const [analyticsResult, shareResult, teamsResult] = await Promise.allSettled([
    fetchAnalytics(period, 'team'),
    fetchModelShare(period, 'all', client),
    fetchTeamOptions(),
  ]);

  const analytics =
    analyticsResult.status === 'fulfilled' ? analyticsResult.value : { trends: [] };
  const initialShare =
    shareResult.status === 'fulfilled'
      ? shareResult.value
      : { period, team_id: 'all', total_cost_usd: 0, models: [] };
  const teams = teamsResult.status === 'fulfilled' ? teamsResult.value : [];

  return (
    <section className="space-y-3">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        {t('trendAndDistribution')}
      </h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-[1.6fr_1fr]">
        <CostTrendCard trends={analytics.trends ?? []} />
        <ModelShareDonutClient initialData={initialShare} teams={teams} period={period} client={client} />
      </div>
    </section>
  );
}

async function TeamUserRanking({ period, client }: { period: string; client: string }) {
  const t = await getTranslations('dashboard');
  // §60.9: 팀·사용자 모두 실제 비용(usage_logs SUCCESS+KST) 기준으로 통일 — 예산
  // 설정 여부와 무관히 진짜 top spender 를 보여준다(기존 budgets/summary 소스는
  // 예산설정 대상만 포함해 누락 위험). 예산 소진율(%)은 budgets/summary 에서 보강.
  const [topTeamsResult, topUsersResult, budgetResult] = await Promise.allSettled([
    fetchTopTeams(period, 5, client),
    fetchTopUsers(period, 5, client),
    fetchBudgetSummary(period),
  ]);
  const topTeams = topTeamsResult.status === 'fulfilled' ? topTeamsResult.value : [];
  const topUsers = topUsersResult.status === 'fulfilled' ? topUsersResult.value : [];
  const budgetItems = budgetResult.status === 'fulfilled' ? budgetResult.value : [];

  // 팀명 → 예산 소진율 룩업(예산 설정된 팀만 존재; 없으면 null).
  const teamPctByName = new Map<string, number | null>(
    budgetItems
      .filter((i) => i.target_type === 'team')
      .map((i) => [i.target_name || '', i.usage_pct != null ? parseFloat(i.usage_pct) : null]),
  );

  const teamRows: TopSpendRow[] = topTeams.map((t) => ({
    id: t.name,
    name: t.name,
    usedUsd: t.cost_usd,
    usagePct: teamPctByName.get(t.name) ?? null,
  }));

  // 실제 비용 기준 — usagePct 는 예산 미설정자가 많아 의미 없어 미표시(null).
  const userRows: TopSpendRow[] = topUsers.map((u) => ({
    id: u.email,
    name: u.name || u.email,
    usedUsd: u.cost_usd,
    usagePct: null,
  }));

  return (
    <section className="space-y-3">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        {t('teamUserRanking')}
      </h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <TopSpendTable
          title={t('topTeamByCost')}
          subtitle={t('topTeamByCostSubtitle')}
          rows={teamRows}
          accentVar="var(--chart-1)"
        />
        <TopSpendTable
          title={t('topUserByCost')}
          subtitle={t('topUserByCostSubtitle')}
          rows={userRows}
          accentVar="var(--chart-2)"
        />
      </div>
    </section>
  );
}

async function ClientDistribution({ period }: { period: string }) {
  const t = await getTranslations('dashboard');
  const share = await fetchClientShare(period).catch(() => null);
  const data: ClientShareResponse = share ?? { period, total_cost_usd: 0, clients: [] };
  return (
    <section className="space-y-3">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        {t('clientCostShare')}
      </h2>
      <div className="glass glass-hover rounded-apple p-5">
        <ClientShareDonutClient data={data} />
      </div>
    </section>
  );
}

const ALLOWED_CLIENTS = ['all', 'claude-code', 'cowork', 'codex', 'other'];

export default async function DashboardPage({
  searchParams,
}: {
  searchParams: { period?: string; client?: string };
}) {
  // 기간 해석은 한 번만. ?period 가 데이터 있는 월이면 존중, 아니면 latest(데이터
  // 있는 가장 최근 월)로. Next 14.2.29 — searchParams 는 sync 객체(await 금지).
  const { periods, latest } = await fetchAvailablePeriods();
  const requested = searchParams?.period;
  const period = requested && periods.includes(requested) ? requested : latest;

  const rawClient = searchParams?.client;
  const client = rawClient && ALLOWED_CLIENTS.includes(rawClient) ? rawClient : 'all';

  const t = await getTranslations('dashboard');

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-bold tracking-tight">{t('title')}</h1>
        <div className="flex flex-wrap items-center gap-2">
          <ClientFilter current={client} />
          <PeriodSelector periods={periods} current={period} />
        </div>
      </div>

      <Suspense
        key={`kpi-${period}-${client}`}
        fallback={
          <div className="space-y-4">
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
              <SkeletonCard count={4} />
            </div>
            <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
              <SkeletonCard count={4} />
            </div>
          </div>
        }
      >
        <DashboardKPIs period={period} client={client} />
      </Suspense>

      <Suspense
        key={`trend-${period}-${client}`}
        fallback={<div className="glass rounded-apple h-72 animate-pulse" />}
      >
        <TrendAndDistribution period={period} client={client} />
      </Suspense>

      <Suspense
        key={`rank-${period}-${client}`}
        fallback={<div className="glass rounded-apple h-64 animate-pulse" />}
      >
        <TeamUserRanking period={period} client={client} />
      </Suspense>

      <Suspense
        key={`client-${period}`}
        fallback={<div className="glass rounded-apple h-64 animate-pulse" />}
      >
        <ClientDistribution period={period} />
      </Suspense>
    </div>
  );
}
