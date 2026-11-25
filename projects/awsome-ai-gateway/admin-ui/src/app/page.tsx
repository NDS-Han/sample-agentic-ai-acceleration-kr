// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { KPICard } from '@/components/common/KPICard';
import { ErrorState } from '@/components/common/ErrorState';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import { errorRateInfo } from '@/lib/utils/errorRate';
import { AlertLevel } from '@/types/enums';
import {
  Key,
  Cpu,
  Activity,
  Coins,
} from 'lucide-react';
import { Suspense } from 'react';
import { getTranslations } from 'next-intl/server';
import {
  fetchDashboardKPI,
  fetchModelShare,
  fetchTeamOptions,
  fetchAnalytics,
  fetchBudgetSummary,
  fetchTopUsers,
  fetchTopTeams,
  fetchAvailablePeriods,
  fetchClientShare,
  fetchDashboardWebSearch,
  type ClientShareResponse,
} from '@/lib/actions/dashboard';
import { ModelShareDonutClient } from '@/components/dashboard/ModelShareDonutClient';
import { ClientShareDonutClient } from '@/components/dashboard/ClientShareDonutClient';
import { CostTrendCard } from '@/components/dashboard/CostTrendCard';
import { monthRange } from '@/lib/utils/trendSeries';
import { TopSpendTable, type TopSpendRow } from '@/components/dashboard/TopSpendTable';
import { PeriodSelector } from '@/components/dashboard/PeriodSelector';
import { ClientFilter } from '@/components/dashboard/ClientFilter';
import { fmtTokensCompact } from '@/lib/utils/format';
import { CostBudgetCards } from '@/components/dashboard/CostBudgetCards';
import { WebSearchSection } from '@/components/dashboard/WebSearchSection';
import { teamDisplayName } from '@/lib/utils/trendSeries';
import { isSyntheticEmail, userLabel } from '@/lib/userLabel';
import { pageTitle } from '@/lib/pageMetadata';
import { DataFreshness } from '@/components/common/DataFreshness';



// 예산 경보 임계값의 단일 출처는 lib/utils/alertLevel — 백엔드 _alert_level
// (>=90 CRITICAL / >=70 WARNING)과 동일. 별도 임계를 두면 대시보드 카드와
// /budgets 표가 같은 수치에 다른 심각도를 표시한다.

// 토큰 포맷의 단일 출처는 lib/utils/format 의 fmtTokensCompact — 로컬 구현을
// 두면 같은 토큰 수가 페이지마다 다른 단위(1050M vs 1.1B)로 표시된다.

async function DashboardKPIs({ period, client }: { period: string; client: string }) {
  const t = await getTranslations('dashboard');

  // 카드 전체가 단일 엔드포인트에서 온다. 예전에는 4개를 Promise.allSettled 로 동시에
  // 불렀고 그중 /admin/budgets/summary 가 예산 config 하나당 Redis GET + SQL SUM 을
  // 순차로 돌아 사용자 수에 비례해 느려지는 병목이었다(수백~수천 명 규모에서 수십 초).
  //
  // ⚠️ 단일 호출은 "한 번 실패하면 카드가 전부 빈다" 는 대가가 있다. 예전 구조는 카드별
  //    degradation 이 가능했다. 그래서 실패를 0 으로 접지 않고 **전부 '—' + fetchFailed**
  //    로 렌더한다 — 부분적으로 그럴듯한 화면보다 "지금 값을 모른다" 가 정확하다.
  const [kpi, webSearch] = await Promise.all([
    fetchDashboardKPI(period, client).catch(() => null),
    // 웹 검색 섹션 — 실패해도 KPI 는 그린다(섹션만 숨김). 표시 규칙은 섹션이 판단.
    fetchDashboardWebSearch(period, client).catch(() => null),
  ]);

  // 앱 필터를 드릴다운 링크로 보존한다 — 없으면 KPI 카드를 눌러 이동한
  // /analytics 가 전체 앱 데이터를 보여 화면 간 수치가 어긋난다(R25-N-UI4).
  const clientSuffix = client !== 'all' ? `&client=${encodeURIComponent(client)}` : '';

  return (
    <div className="space-y-6">
      {/* KPI 단일 엔드포인트 실패 시 '—' 카드와 함께 재시도 수단을 제공한다. */}
      {!kpi && <ErrorState compact />}
      {/* 집계 기준 시각 + 지연 날짜 칩 — 하이브리드 롤업 읽기 메타(KPI 응답). */}
      {kpi && <DataFreshness meta={kpi} className="-mt-2 px-1" />}
      {/* ── 비용 & 예산 ── */}
      <section className="space-y-3">
        <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
          {t('costAndBudget')}
        </h2>
        <CostBudgetCards kpi={kpi} period={period} client={client} />
      </section>

      {/* ── 사용량 & 시스템 ── */}
      <section className="space-y-3">
        <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
          {t('usageAndSystem')}
        </h2>
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-4">
          {(() => {
            // 요청 카드: 값은 성공 수(비용·토큰 KPI 와 같은 모집단), 서브라인에
            // 전체·에러를 싣는다. 에러가 있으면 상세는 모니터링이 맞는 목적지다.
            const err = errorRateInfo(kpi?.total_requests ?? 0, kpi?.error_requests ?? 0);
            return (
              <KPICard
                title={t('totalRequests')}
                value={kpi ? kpi.total_requests.toLocaleString() : '—'}
                icon={<Activity size={18} aria-hidden="true" />}
                alertLevel={err.warn ? AlertLevel.WARNING : AlertLevel.NORMAL}
                alertLabel={err.warn ? t('errorRateLabel', { rate: err.rate.toFixed(1) }) : undefined}
                description={
                  kpi
                    ? err.warn
                      ? t('requestsSublineWarn', {
                          total: err.total.toLocaleString(),
                          errors: err.errors.toLocaleString(),
                        })
                      : t('requestsSubline', {
                          total: err.total.toLocaleString(),
                          errors: err.errors.toLocaleString(),
                          rate: err.rate.toFixed(1),
                        })
                    : t('fetchFailed')
                }
                href={err.warn ? '/monitoring' : `/analytics?period=${period}${clientSuffix}`}
              />
            );
          })()}
          <KPICard
            title={t('totalTokens')}
            value={kpi ? fmtTokensCompact(kpi.total_tokens) : '—'}
            icon={<Coins size={18} aria-hidden="true" />}
            description={t('totalTokensDesc')}
            href={`/analytics?period=${period}${clientSuffix}`}
          />
          <KPICard
            title={t('activeKeys')}
            value={kpi ? kpi.active_keys.toLocaleString() : '—'}
            icon={<Key size={18} aria-hidden="true" />}
            description={kpi ? t('activeKeysDesc') : t('fetchFailed')}
            href="/keys"
          />
          <KPICard
            title={t('activeModels')}
            value={kpi ? kpi.active_models.toLocaleString() : '—'}
            icon={<Cpu size={18} aria-hidden="true" />}
            description={kpi ? t('activeModelsDesc') : t('fetchFailed')}
            href="/models"
          />
        </div>
      </section>

      {/* ── 웹 검색 · AgentCore ── (표시 규칙이 거짓이면 섹션 자체를 그리지 않는다) */}
      {webSearch && <WebSearchSection data={webSearch} period={period} />}
    </div>
  );
}

async function TrendAndDistribution({ period, client }: { period: string; client: string }) {
  const t = await getTranslations('dashboard');
  const [analyticsResult, shareResult, teamsResult] = await Promise.allSettled([
    fetchAnalytics(period, 'team', client),
    fetchModelShare(period, 'all', client),
    fetchTeamOptions(),
  ]);

  const analytics =
    analyticsResult.status === 'fulfilled' ? analyticsResult.value : { trends: [], trends_by_team: [] };
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
        {/* 섹션별 실패를 조용한 빈 차트 대신 재시도 가능한 에러 상태로 표시 */}
        {analyticsResult.status === 'fulfilled' ? (
          <CostTrendCard
            trends={analytics.trends ?? []}
            trendsByTeam={analytics.trends_by_team ?? []}
            range={monthRange(period)}
          />
        ) : (
          <ErrorState />
        )}
        {shareResult.status === 'fulfilled' ? (
          <ModelShareDonutClient initialData={initialShare} teams={teams} period={period} client={client} />
        ) : (
          <ErrorState />
        )}
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

  // target_id(팀/사용자 uuid) 기준 예산 소진율/한도 룩업 — 이름 매칭은 부서 접두어
  // 유무 등으로 어긋날 수 있어 id 기준이 안전(§60.9 팔로업).
  const teamPctById = new Map<string, number | null>(
    budgetItems
      .filter((i) => i.target_type === 'team')
      .map((i) => [i.target_id, i.usage_pct != null ? parseFloat(i.usage_pct) : null]),
  );
  // target_id(팀 uuid) → 예산 한도/사용액. "팀 예산 적용" 표기 시 "얼마나 쓰고
  // 있는지"까지 병기하기 위함(팀명만으론 한눈에 안 보인다는 피드백 반영).
  // (팀명 매칭은 부서 접두어 유무 등으로 어긋날 수 있어 id 기준이 안전).
  const teamLimitById = new Map<string, number | null>(
    budgetItems
      .filter((i) => i.target_type === 'team')
      .map((i) => [i.target_id, i.limit_usd != null ? parseFloat(i.limit_usd) : null]),
  );
  const teamUsedById = new Map<string, number>(
    budgetItems
      .filter((i) => i.target_type === 'team')
      .map((i) => [i.target_id, parseFloat(i.used_usd || '0')]),
  );
  const userPctById = new Map<string, number | null>(
    budgetItems
      .filter((i) => i.target_type === 'user')
      .map((i) => [i.target_id, i.usage_pct != null ? parseFloat(i.usage_pct) : null]),
  );

  const teamRows: TopSpendRow[] = topTeams.map((tm) => ({
    id: tm.team_id,
    // 동명 팀 구분 — 예산 표와 같은 canonical 규칙(부서_팀).
    name: teamDisplayName({ team: tm.name, dept_name: tm.department_name }),
    usedUsd: tm.cost_usd,
    usagePct: teamPctById.get(tm.team_id) ?? null,
    href: `/budgets?team=${tm.team_id}`,
  }));

  // 실제 비용 기준. 본인 예산이 없더라도(usagePct=null) 소속 팀에 예산이 설정돼
  // 있으면 "팀 예산 적용"으로 표기(§60.9 팔로업) — 완전 무예산과 구분. 소속 팀명을
  // 이름 아래 병기하고, 팀 예산 적용 시엔 팀의 사용액/한도도 함께 보여줘 어떤
  // 유저가 어떤 팀에서 얼마를 쓰는지 한눈에 보이게 한다.
  // 표시 이름이 겹치는 유저(예: 'Load Test' 2명)는 보조 줄에 이메일을 같이 적어 구분한다.
  const displayName = (u: (typeof topUsers)[number]) => u.name || userLabel({ email: u.email });
  const nameCount = new Map<string, number>();
  for (const u of topUsers) nameCount.set(displayName(u), (nameCount.get(displayName(u)) ?? 0) + 1);

  const userRows: TopSpendRow[] = topUsers.map((u) => {
    const name = displayName(u);
    const team = u.team_name
      ? teamDisplayName({ team: u.team_name, dept_name: u.department_name })
      : null;
    // 합성 이메일(<sub>@unknown)은 구분자로도 노출하지 않는다.
    const email =
      (nameCount.get(name) ?? 0) > 1 && u.email && !isSyntheticEmail(u.email) ? u.email : null;
    const usagePct = userPctById.get(u.user_id) ?? null;
    const teamLimit = u.team_id ? teamLimitById.get(u.team_id) ?? null : null;
    const teamBudgetApplied = usagePct == null && !!u.team_id && teamLimit != null;
    return {
      id: u.user_id,
      // 이름이 없을 때 합성 이메일(<sub>@unknown)을 그대로 노출하지 않는다.
      name,
      subtitle: [email, team].filter(Boolean).join(' · ') || null,
      usedUsd: u.cost_usd,
      usagePct,
      href: `/users?node=${u.user_id}`,
      teamBudgetApplied,
      teamBudget:
        teamBudgetApplied && u.team_id && teamLimit != null
          ? { used: teamUsedById.get(u.team_id) ?? 0, limit: teamLimit }
          : null,
    };
  });

  return (
    <section className="space-y-3">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        {t('teamUserRanking')}
      </h2>
      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        {topTeamsResult.status === 'fulfilled' ? (
          <TopSpendTable
            title={t('topTeamByCost')}
            subtitle={t('topTeamByCostSubtitle')}
            rows={teamRows}
            accentVar="var(--chart-1)"
            teamBudgetAppliedLabel={t('teamBudgetApplied')}
          />
        ) : (
          <ErrorState />
        )}
        {topUsersResult.status === 'fulfilled' ? (
          <TopSpendTable
            title={t('topUserByCost')}
            subtitle={t('topUserByCostSubtitle')}
            rows={userRows}
            accentVar="var(--chart-2)"
            teamBudgetAppliedLabel={t('teamBudgetApplied')}
          />
        ) : (
          <ErrorState />
        )}
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
        {share ? <ClientShareDonutClient data={data} /> : <ErrorState compact />}
      </div>
    </section>
  );
}

const ALLOWED_CLIENTS = ['all', 'claude-code', 'cowork', 'codex', 'other'];

export const generateMetadata = pageTitle('nav', 'dashboard');

export default async function DashboardPage(
  props: {
    searchParams: Promise<{ period?: string; client?: string }>;
  }
) {
  const searchParams = await props.searchParams;
  // 기간 해석은 한 번만. ?period 가 데이터 있는 월이면 존중, 아니면 latest(데이터
  // 있는 가장 최근 월)로. Next 15 — searchParams 는 Promise(위에서 await).
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
