'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState } from 'react';
import { useTranslations } from 'next-intl';
import { Activity, ChevronDown, ChevronRight, Coins, DollarSign } from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import type { MyBudgetResponse, MyUsageResponse, ModelUsage } from '@/lib/actions/my';
import { fmtUsd } from '@/lib/utils/format';
import { modelDisplay } from '@/lib/utils/modelLabel';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { TokenMixDonutClient, TOKEN_BUCKETS } from '@/components/analytics/TokenMixDonutClient';
import { KPICard } from '@/components/common/KPICard';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';
import { ErrorState } from '@/components/common/ErrorState';

const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });

/** 행의 4버킷 분해 — 모델 표의 미니 바·펼침 상세가 공유한다. */
function rowBucket(row: ModelUsage, key: (typeof TOKEN_BUCKETS)[number]['key']): number {
  return key === 'cache_write_tokens' ? row.cache_creation_tokens : row[key];
}

export function MyUsageDashboard({
  data,
  budget,
}: {
  data: MyUsageResponse;
  budget: MyBudgetResponse | null;
}) {
  const t = useTranslations('my');
  const [trendMetric, setTrendMetric] = useState<'cost_usd' | 'tokens'>('cost_usd');
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(new Set());

  // summary 는 서버 단일 집계 — 일별 합산과 같은 §59 스코프라 카드 값이 정확하다.
  const totalCost = data.summary?.cost_usd ?? data.daily_usage.reduce((sum, d) => sum + d.cost_usd, 0);
  const totalRequests = data.summary?.requests ?? data.daily_usage.reduce((sum, d) => sum + d.requests, 0);
  const s = data.summary;
  const totalTokens = s
    ? s.input_tokens + s.output_tokens + s.cache_creation_tokens + s.cache_read_tokens
    : data.daily_usage.reduce((sum, d) => sum + d.tokens, 0);

  const toggleExpand = (alias: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(alias)) next.delete(alias);
      else next.add(alias);
      return next;
    });
  };

  return (
    <div className="space-y-6">
      {/* Row 1 — 예산 카드(이번 달 고정) + KPI + 일별 추이 */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        <div className="lg:col-span-1">
          {budget ? (
            <MyBudgetCard data={budget} basisMismatch={budget.period !== data.period} />
          ) : (
            <ErrorState compact />
          )}
        </div>
        <div className="lg:col-span-2 flex flex-col gap-6">
          <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
            <KPICard
              title={t('totalCost')}
              value={fmtUsd(totalCost)}
              icon={<DollarSign size={18} />}
              description={t('kpiCostDesc', { requests: totalRequests.toLocaleString() })}
            />
            <KPICard
              title={t('totalRequests')}
              value={totalRequests.toLocaleString()}
              icon={<Activity size={18} />}
              description={t('kpiRequestsDesc', { count: data.by_model.length })}
            />
            <KPICard
              title={t('totalTokens')}
              value={compact.format(totalTokens)}
              icon={<Coins size={18} />}
              description={
                s
                  ? t('kpiTokensDesc', {
                      input: compact.format(s.input_tokens),
                      output: compact.format(s.output_tokens),
                    })
                  : t('totalTokensDesc')
              }
            />
          </div>

          {/* 일별 추이 — 비용/토큰 토글. 표는 details 로 접어 둔다(정확값용). */}
          <div className="glass rounded-apple p-4 flex-1">
            <div className="mb-3 flex items-center justify-between">
              <h3 className="text-sm font-semibold">{t('dailyUsage')}</h3>
              <div className="flex rounded-md border border-border text-xs">
                {(['cost_usd', 'tokens'] as const).map((m) => (
                  <button
                    key={m}
                    onClick={() => setTrendMetric(m)}
                    className={`px-3 py-1 ${m === trendMetric ? 'bg-accent font-medium' : 'text-muted-foreground'}`}
                  >
                    {t(m === 'cost_usd' ? 'trendCost' : 'trendTokens')}
                  </button>
                ))}
              </div>
            </div>
            {data.daily_usage.length > 0 ? (
              <>
                <ResponsiveContainer width="100%" height={220}>
                  <AreaChart data={data.daily_usage}>
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" />
                    <XAxis
                      dataKey="date"
                      tick={{ fontSize: 11 }}
                      stroke="hsl(var(--muted-foreground))"
                      tickFormatter={(d: string) => d.slice(5)}
                    />
                    <YAxis
                      tick={{ fontSize: 11 }}
                      stroke="hsl(var(--muted-foreground))"
                      tickFormatter={(v: number) =>
                        trendMetric === 'cost_usd' ? fmtUsd(v) : compact.format(v)
                      }
                    />
                    <Tooltip
                      contentStyle={{
                        backgroundColor: 'hsl(var(--card))',
                        border: '1px solid hsl(var(--border))',
                        borderRadius: '12px',
                        fontSize: '12px',
                      }}
                      labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
                      formatter={((value: number) => [
                        trendMetric === 'cost_usd'
                          ? fmtUsd(value)
                          : `${value.toLocaleString()} ${t('tokUnit')}`,
                        t(trendMetric === 'cost_usd' ? 'keyCost' : 'trendTokens'),
                      ]) as never}
                    />
                    <Area
                      type="monotone"
                      dataKey={trendMetric}
                      stroke="hsl(var(--chart-1))"
                      fill="hsl(var(--chart-1))"
                      fillOpacity={0.25}
                      dot={data.daily_usage.length <= 3}
                    />
                  </AreaChart>
                </ResponsiveContainer>
                <details className="mt-3 group">
                  <summary className="cursor-pointer select-none text-xs text-muted-foreground hover:text-foreground">
                    {t('dailyDetail')}
                  </summary>
                  <div className="mt-2 max-h-56 overflow-y-auto">
                    <Table density="compact">
                      <THead>
                        <Tr>
                          <Th>{t('colDate')}</Th>
                          <Th numeric>{t('colCostUsd')}</Th>
                          <Th numeric>{t('colRequests')}</Th>
                          <Th numeric>{t('colTokens')}</Th>
                        </Tr>
                      </THead>
                      <TBody>
                        {data.daily_usage.map((row) => (
                          <Tr key={row.date}>
                            <Td className="num">{row.date}</Td>
                            <Td numeric>{fmtUsd(row.cost_usd)}</Td>
                            <Td numeric>{row.requests.toLocaleString()}</Td>
                            <Td numeric>{row.tokens.toLocaleString()}</Td>
                          </Tr>
                        ))}
                      </TBody>
                    </Table>
                  </div>
                </details>
              </>
            ) : (
              <p className="py-12 text-center text-sm text-muted-foreground">
                {t('noDailyRecords')}
              </p>
            )}
          </div>
        </div>
      </div>

      {/* Row 2 — 모델별 사용량(3/5) + 토큰 분석(2/5) */}
      <div className="grid grid-cols-1 lg:grid-cols-5 gap-6">
        <div className="glass rounded-apple p-4 lg:col-span-3">
          <h3 className="mb-3 text-sm font-semibold">{t('modelUsage')}</h3>
          {data.by_model.length === 0 ? (
            <p className="py-8 text-center text-sm text-muted-foreground">{t('noModelRecords')}</p>
          ) : (
            <Table density="compact">
              <THead>
                <Tr>
                  <Th>{t('colModel')}</Th>
                  <Th numeric>{t('colRequests')}</Th>
                  <Th>{t('colTokenMix')}</Th>
                  <Th numeric>{t('colCostUsd')}</Th>
                  <Th numeric>{t('colShare')}</Th>
                </Tr>
              </THead>
              <TBody>
                {data.by_model.map((row) => {
                  const isOpen = expanded.has(row.model_alias);
                  const share = totalCost > 0 ? (row.cost_usd / totalCost) * 100 : null;
                  return [
                    <Tr key={row.model_alias}>
                      <Td emphasis className="text-xs">
                        <button
                          type="button"
                          onClick={() => toggleExpand(row.model_alias)}
                          aria-expanded={isOpen}
                          className="inline-flex items-center gap-1 text-left hover:text-foreground"
                        >
                          {isOpen ? (
                            <ChevronDown size={13} className="text-muted-foreground" />
                          ) : (
                            <ChevronRight size={13} className="text-muted-foreground" />
                          )}
                          <span title={row.model_alias}>
                            {modelDisplay(row.model_alias, row.display_name)}
                          </span>
                        </button>
                      </Td>
                      <Td numeric>{row.requests.toLocaleString()}</Td>
                      <Td>
                        <div className="flex items-center gap-2">
                          <div className="flex h-1.5 w-24 overflow-hidden rounded-full bg-muted">
                            {TOKEN_BUCKETS.map((b) => {
                              const pct =
                                row.tokens > 0 ? (rowBucket(row, b.key) / row.tokens) * 100 : 0;
                              return (
                                <span
                                  key={b.key}
                                  style={{ width: `${pct}%`, backgroundColor: b.color }}
                                />
                              );
                            })}
                          </div>
                          <span className="num text-xs text-muted-foreground">
                            {compact.format(row.tokens)}
                          </span>
                        </div>
                      </Td>
                      <Td numeric>{fmtUsd(row.cost_usd)}</Td>
                      <Td numeric className="text-muted-foreground">
                        {share != null ? `${share.toFixed(1)}%` : '—'}
                      </Td>
                    </Tr>,
                    isOpen ? (
                      <Tr key={`${row.model_alias}-detail`} className="bg-muted/30">
                        <Td colSpan={5} className="text-xs text-muted-foreground">
                          <span className="inline-flex flex-wrap gap-x-4 gap-y-1">
                            {TOKEN_BUCKETS.map((b) => (
                              <span key={b.key} className="inline-flex items-center gap-1.5">
                                <span
                                  className="h-2 w-2 rounded-full"
                                  style={{ backgroundColor: b.color }}
                                />
                                {t(
                                  b.key === 'input_tokens'
                                    ? 'colTokInput'
                                    : b.key === 'output_tokens'
                                      ? 'colTokOutput'
                                      : b.key === 'cache_write_tokens'
                                        ? 'colTokCacheWrite'
                                        : 'colTokCacheRead'
                                )}
                                <span className="num">{rowBucket(row, b.key).toLocaleString()}</span>
                              </span>
                            ))}
                          </span>
                        </Td>
                      </Tr>
                    ) : null,
                  ];
                })}
              </TBody>
            </Table>
          )}
        </div>

        {/* 토큰 분석 — stacked(세로) 배치: 2/5 칸은 가로 배치엔 좁다 */}
        <div className="glass rounded-apple p-4 lg:col-span-2">
          <h3 className="mb-3 text-sm font-semibold">{t('tokTitle')}</h3>
          {s && totalTokens > 0 ? (
            <TokenMixDonutClient
              layout="stacked"
              data={{
                input_tokens: s.input_tokens,
                output_tokens: s.output_tokens,
                cache_read_tokens: s.cache_read_tokens,
                cache_write_tokens: s.cache_creation_tokens,
                total_tokens: totalTokens,
              }}
            />
          ) : (
            <p className="py-12 text-center text-sm text-muted-foreground">
              {t('noDailyRecords')}
            </p>
          )}
          {s && (s.reasoning_tokens > 0 || s.web_search_count > 0) && (
            <p className="mt-3 border-t border-border pt-2 text-xs text-muted-foreground">
              {s.reasoning_tokens > 0 &&
                t('tokReasoning', { n: s.reasoning_tokens.toLocaleString() })}
              {s.reasoning_tokens > 0 && s.web_search_count > 0 && ' · '}
              {s.web_search_count > 0 &&
                t('tokWebSearch', { n: s.web_search_count.toLocaleString() })}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
