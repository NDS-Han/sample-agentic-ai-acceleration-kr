'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 대시보드 'WEB SEARCH · AGENTCORE' 섹션 — 넓은 카드 하나, 세 칸.
 *
 *  1) 앱별 검색 수 도넛(앱 점유·모델 점유와 같은 도넛) + 범례 "앱 · 수 · %"
 *  2) 'Web search cost' KPI(ⓘ 무료분·정가 툴팁, 부제 "<N> queries × $<price> per 1,000")
 *     + 'Avg per day'(검색 수/경과일) · 'Per request'(검색 수/성공 요청, 소수 2자리)
 *  3) 'Daily queries' 막대 — 비용 추이와 같은 날짜 규칙(오늘 이후 빈칸, 지난 빈 날은 평평한 막대)
 *
 * 표시 규칙: 배포됐거나 기록 비용 > 0. 배포 안 됐는데 비용이 있으면 "현재 미배포" 배지.
 * 둘 다 아니면 섹션을 그리지 않는다. 무료분·예산 안내 문장은 보이지 않게(툴팁만).
 */

import { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Bar, BarChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';

import { Badge } from '@/components/common/Badge';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { WebSearchCostValue, WebSearchPriceInfo } from '@/components/common/WebSearchCost';
import { ShareDonut } from '@/components/dashboard/ClientShareDonutClient';
import type { DashboardWebSearch } from '@/lib/actions/dashboard';
import { CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';
import { fmtSharePct, fmtSmallRatio, fmtUsdRate, roundSharesTo100 } from '@/lib/utils/format';
import { labelFor } from '@/lib/utils/modelLabel';
import { reportingNowParts } from '@/lib/utils/period';
import { buildDailyQuerySeries } from '@/lib/utils/webSearchDaily';
import { webSearchNotDeployed, webSearchVisible } from '@/lib/utils/webSearchVisibility';

const COLORS = CATEGORICAL_PALETTE;
const EMPTY_RING = [{ name: '', value: 1, fill: 'hsl(var(--muted))' }];
/** 도넛 정사각 px — 이 카드는 세 칸이 한 줄이라 앱/모델 점유 도넛(256)보다 작게. */
const DONUT_PX = 152;
// 가운데 글자 상자 — 구멍 지름(innerRadius 62% × 반지름 76 = 47px → 94px)보다 좁게.
// 두 줄(숫자 아래 ±12px)에서도 링에 닿지 않는 폭: 2·√(47²−12²) ≈ 91px → 여유 두고 72px.
const DONUT_TEXT_PX = 72;

function Stat({ label, value, testId }: { label: string; value: string; testId: string }) {
  return (
    <div className="min-w-0">
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="mt-0.5 text-lg font-semibold tabular-nums" data-testid={testId}>
        {value}
      </p>
    </div>
  );
}

export function WebSearchSection({
  data,
  period,
  today: todayOverride,
}: {
  data: DashboardWebSearch;
  period: string;
  /** 테스트용 — 리포팅 TZ 기준 오늘(YYYY-MM-DD). */
  today?: string;
}) {
  const t = useTranslations('webSearchCost');
  const td = useTranslations('dashboard');
  const tz = useReportingTz();
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const otherLabel = td('clientOther');

  const today = useMemo(() => {
    if (todayOverride) return todayOverride;
    const { y, m, d } = reportingNowParts(tz);
    return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
  }, [tz, todayOverride]);
  const pieData = useMemo(
    () =>
      data.by_client.map((c, i) => ({
        name: labelFor(c.client, otherLabel),
        value: c.queries,
        fill: COLORS[i % COLORS.length],
      })),
    [data, otherLabel],
  );
  const shares = useMemo(() => {
    const total = data.by_client.reduce((s, c) => s + c.queries, 0);
    const raw = data.by_client.map((c) => (total > 0 ? (c.queries / total) * 100 : 0));
    return { raw, rounded: roundSharesTo100(raw) };
  }, [data]);
  const daily = useMemo(
    () => buildDailyQuerySeries(period, data.daily, today),
    [period, data.daily, today],
  );

  const visible = webSearchVisible({ deployed: data.deployed, cost_usd: data.cost_usd });
  if (!visible) return null;
  const notDeployed = webSearchNotDeployed({ deployed: data.deployed, cost_usd: data.cost_usd });
  const price = Number(data.price_per_1k_queries);
  const hovered = hoverIndex != null ? data.by_client[hoverIndex] : null;
  const fmtNum = (n: number | null, digits?: number) =>
    n == null
      ? '—'
      : digits != null
        ? n.toFixed(digits)
        : n.toLocaleString('en-US', { maximumFractionDigits: 2 });

  return (
    <section className="space-y-3" data-testid="web-search-section">
      <h2 className="px-1 text-xs font-bold uppercase tracking-[0.12em] text-muted-foreground">
        {t('sectionLabel')}
      </h2>
      <div className="glass glass-hover rounded-apple p-5">
        <div className="mb-4 flex flex-wrap items-center gap-2">
          <h3 className="text-sm font-semibold">{t('cardTitle')}</h3>
          {notDeployed && (
            <Badge tone="amber" className="text-[11px]">
              <span data-testid="web-search-not-deployed">{t('notDeployedBadge')}</span>
            </Badge>
          )}
        </div>
        <div className="grid grid-cols-1 gap-6 lg:grid-cols-[auto_minmax(0,0.9fr)_minmax(0,1.3fr)]">
          {/* 1) 앱별 검색 수 */}
          <div className="flex min-w-0 flex-col items-center gap-4 sm:flex-row sm:items-center">
            <div
              className="relative shrink-0"
              style={{ width: DONUT_PX, height: DONUT_PX }}
              data-testid="web-search-donut"
            >
              <ShareDonut data={data.queries > 0 ? pieData : EMPTY_RING} onHover={setHoverIndex} size={DONUT_PX} />
              <div className="pointer-events-none absolute inset-0 flex items-center justify-center">
                <div className="flex flex-col items-center text-center" style={{ width: DONUT_TEXT_PX }}>
                  <p className="text-xl font-bold leading-none tabular-nums" data-testid="web-search-center">
                    {(hovered ? hovered.queries : data.queries).toLocaleString('en-US')}
                  </p>
                  <p className="mt-1 line-clamp-2 break-words text-[11px] leading-tight text-muted-foreground" data-testid="web-search-center-label">
                    {hovered ? labelFor(hovered.client, otherLabel) : t('queriesThisMonth')}
                  </p>
                </div>
              </div>
            </div>
            <ul className="w-full min-w-0 space-y-1.5 text-sm sm:w-auto sm:min-w-[11rem] sm:flex-1" data-testid="web-search-legend">
              {data.by_client.map((c, i) => (
                <li
                  key={c.client}
                  className={`grid grid-cols-[minmax(0,1fr)_auto_3.5rem] items-baseline gap-2 rounded-apple-sm px-2 py-0.5 -mx-2 ${i === hoverIndex ? 'bg-accent' : ''}`}
                >
                  <span className="flex min-w-0 items-center gap-2">
                    <span
                      className="h-2.5 w-2.5 shrink-0 rounded-full"
                      style={{ backgroundColor: COLORS[i % COLORS.length] }}
                      aria-hidden="true"
                    />
                    <span className="truncate font-medium">{labelFor(c.client, otherLabel)}</span>
                  </span>
                  <span className="text-xs tabular-nums text-muted-foreground">
                    {c.queries.toLocaleString('en-US')}
                  </span>
                  <span className="text-right text-xs tabular-nums text-muted-foreground">
                    {fmtSharePct(shares.rounded[i], shares.raw[i])}
                  </span>
                </li>
              ))}
            </ul>
          </div>

          {/* 2) 비용 KPI + 일평균·요청당 */}
          <div className="flex flex-col justify-center gap-4 lg:border-l lg:border-border lg:pl-6">
            <div>
              <div className="flex items-center gap-1.5">
                <p className="text-sm font-medium text-muted-foreground">{t('cardTitle')}</p>
                <WebSearchPriceInfo price={data.price_per_1k_queries} />
              </div>
              <p className="mt-1 text-3xl font-bold tabular-nums" data-testid="web-search-cost">
                <WebSearchCostValue cost={data.cost_usd} significant />
              </p>
              <p className="mt-1 text-xs text-muted-foreground" data-testid="web-search-cost-subtitle">
                {t('costSubtitle', { count: data.queries, price: fmtUsdRate(price) })}
              </p>
            </div>
            <div className="grid grid-cols-2 gap-4">
              <Stat label={t('avgPerDay')} value={fmtNum(data.avg_queries_per_day)} testId="web-search-avg-day" />
              <Stat
                label={t('perRequest')}
                value={fmtSmallRatio(data.queries_per_request)}
                testId="web-search-per-request"
              />
            </div>
          </div>

          {/* 3) 일별 검색 수 */}
          <div className="min-w-0 lg:border-l lg:border-border lg:pl-6">
            <p className="mb-2 text-sm font-medium text-muted-foreground">{t('dailyQueries')}</p>
            <div className="h-40" data-testid="web-search-daily">
              <ResponsiveContainer width="100%" height="100%">
                <BarChart data={daily} margin={{ top: 4, right: 4, bottom: 0, left: -20 }}>
                  <XAxis
                    dataKey="label"
                    tick={{ fontSize: 10 }}
                    interval="preserveStartEnd"
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis allowDecimals={false} tick={{ fontSize: 10 }} tickLine={false} axisLine={false} />
                  <Tooltip
                    cursor={{ fill: 'hsl(var(--muted))' }}
                    contentStyle={{
                      backgroundColor: 'hsl(var(--card))',
                      border: '1px solid hsl(var(--border))',
                      borderRadius: '12px',
                      fontSize: '12px',
                    }}
                    labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
                    itemStyle={{ color: 'hsl(var(--foreground))' }}
                    formatter={(v) => [Number(v).toLocaleString('en-US'), t('dailyQueries')]}
                  />
                  {/* minPointSize — 검색 0회인 지난 날도 평평한 막대로 보인다(null=미래는 그리지 않음). */}
                  <Bar dataKey="queries" fill={COLORS[0]} radius={[2, 2, 0, 0]} minPointSize={2} isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}
