'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Activity, Coins, DollarSign, Timer } from 'lucide-react';
import {
  Area,
  AreaChart,
  CartesianGrid,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { Badge } from '@/components/common/Badge';
import { KPICard } from '@/components/common/KPICard';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { PeriodSelector } from '@/components/dashboard/PeriodSelector';
import { TokenMixDonutClient } from '@/components/analytics/TokenMixDonutClient';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDate, fmtUsd } from '@/lib/utils/format';
import { modelDisplay } from '@/lib/utils/modelLabel';
import { monthsAgo } from '@/lib/utils/period';
import type { ServiceAccountUsageResponse } from '@/types/entities';

interface Props {
  data: ServiceAccountUsageResponse;
  period: string;
}

export function ServiceAccountDetail({ data, period }: Props) {
  const t = useTranslations('serviceAccounts');
  const tz = useReportingTz();
  const s = data.summary;
  const [trendMetric, setTrendMetric] = useState<'cost_usd' | 'tokens'>('cost_usd');

  // PeriodSelector 용 최근 12개월 목록(최신순).
  const periods = useMemo(
    () => Array.from({ length: 12 }, (_, i) => monthsAgo(i, tz)),
    [tz],
  );

  const statusTone = (status: string) =>
    status === 'ACTIVE'
      ? ('teal' as const)
      : status === 'EXPIRED'
        ? ('neutral' as const)
        : ('pink' as const);

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-semibold">{t('usageTitle', { period })}</h2>
        <PeriodSelector periods={periods} current={period} />
      </div>

      <p className="text-xs text-muted-foreground">
        {t('attributionNote', {
          since: fmtDate(data.attribution_since, tz),
        })}
      </p>

      {/* KPI 카드 — 비용·토큰은 SUCCESS 행만(§59), 요청 수는 전체 상태 포함. */}
      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <KPICard
          title={t('kpiCost')}
          value={fmtUsd(s.cost_usd)}
          icon={<DollarSign size={18} />}
          description={t('kpiCostDesc', {
            success: s.success.toLocaleString(),
            requests: s.requests.toLocaleString(),
          })}
        />
        <KPICard
          title={t('kpiRequests')}
          value={s.requests.toLocaleString()}
          icon={<Activity size={18} />}
          description={t('kpiRequestsDesc', {
            error: s.error.toLocaleString(),
            timeout: s.timeout.toLocaleString(),
          })}
        />
        <KPICard
          title={t('kpiTokens')}
          value={(
            s.input_tokens +
            s.output_tokens +
            s.cache_creation_tokens +
            s.cache_read_tokens
          ).toLocaleString()}
          icon={<Coins size={18} />}
          description={t('kpiTokensDesc', {
            input: s.input_tokens.toLocaleString(),
            output: s.output_tokens.toLocaleString(),
          })}
        />
        <KPICard
          title={t('kpiLatency')}
          value={s.avg_latency_ms != null ? `${Math.round(s.avg_latency_ms)}ms` : '—'}
          icon={<Timer size={18} />}
          description={
            s.last_request_at
              ? t('kpiLastRequest', { at: fmtDate(s.last_request_at, tz) })
              : '—'
          }
        />
      </div>

      {(s.unattributed_requests > 0 || s.unattributed_cost_usd > 0) && (
        <div className="glass rounded-apple border border-amber-500/40 p-3 text-xs text-amber-600 dark:text-amber-400">
          {t('unattributedNote', {
            count: s.unattributed_requests.toLocaleString(),
            cost: fmtUsd(s.unattributed_cost_usd),
          })}
        </div>
      )}

      {/* 토큰 분석 — 버킷 구성 비율 + 비청구 reasoning. */}
      <div className="glass rounded-apple p-4">
        <h3 className="mb-3 text-sm font-semibold">{t('tokTitle')}</h3>
        {s.input_tokens + s.output_tokens + s.cache_creation_tokens + s.cache_read_tokens > 0 ? (
          <TokenMixDonutClient
            data={{
              input_tokens: s.input_tokens,
              output_tokens: s.output_tokens,
              cache_read_tokens: s.cache_read_tokens,
              cache_write_tokens: s.cache_creation_tokens,
              total_tokens:
                s.input_tokens +
                s.output_tokens +
                s.cache_creation_tokens +
                s.cache_read_tokens,
            }}
          />
        ) : (
          <p className="py-4 text-center text-sm text-muted-foreground">
            {t('noUsage')}
          </p>
        )}
        {(s.reasoning_tokens > 0 || s.web_search_count > 0) && (
          <p className="mt-3 border-t border-border pt-2 text-xs text-muted-foreground">
            {s.reasoning_tokens > 0 &&
              t('tokReasoning', { n: s.reasoning_tokens.toLocaleString() })}
            {s.reasoning_tokens > 0 && s.web_search_count > 0 && ' · '}
            {s.web_search_count > 0 &&
              t('tokWebSearch', { n: s.web_search_count.toLocaleString() })}
          </p>
        )}
      </div>

      {/* 발급된 키들 — 재발급 이력 포함. 원문은 절대 표시하지 않는다. */}
      <div className="glass rounded-apple p-4">
        <h3 className="mb-3 text-sm font-semibold">{t('keysTitle')}</h3>
        <Table>
          <THead>
            <Tr>
              <Th>{t('keyPrefix')}</Th>
              <Th>{t('keyStatus')}</Th>
              <Th>{t('keyIssuedAt')}</Th>
              <Th>{t('keyExpiresAt')}</Th>
              <Th>{t('keyLastRequest')}</Th>
              <Th numeric>{t('keyRequests')}</Th>
              <Th numeric>{t('keyCost')}</Th>
            </Tr>
          </THead>
          <TBody>
            {data.keys.map((k) => (
              <Tr key={k.key_id}>
                <Td>
                  <Badge tone="teal">{k.key_prefix}</Badge>
                </Td>
                <Td>
                  <Badge tone={statusTone(k.status)}>
                    {t(`keyStatus.${k.status}` as 'keyStatus.ACTIVE' | 'keyStatus.EXPIRED' | 'keyStatus.REVOKED')}
                  </Badge>
                </Td>
                <Td className="text-muted-foreground">{fmtDate(k.issued_at, tz)}</Td>
                <Td className="text-muted-foreground">{fmtDate(k.expires_at, tz)}</Td>
                <Td className="text-muted-foreground">
                  {k.last_request_at ? fmtDate(k.last_request_at, tz) : '—'}
                </Td>
                <Td numeric className="text-muted-foreground">
                  {k.requests.toLocaleString()}
                </Td>
                <Td numeric className="text-muted-foreground">
                  {fmtUsd(k.cost_usd)}
                </Td>
              </Tr>
            ))}
            {data.keys.length === 0 && (
              <Tr>
                <Td className="text-muted-foreground" colSpan={7}>
                  {t('noKeys')}
                </Td>
              </Tr>
            )}
          </TBody>
        </Table>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        {/* 모델별 분해 */}
        <div className="glass rounded-apple p-4">
          <h3 className="mb-3 text-sm font-semibold">{t('byModelTitle')}</h3>
          <Table>
            <THead>
              <Tr>
                <Th>{t('colModel')}</Th>
                <Th numeric>{t('keyRequests')}</Th>
                <Th numeric>{t('colTokens')}</Th>
                <Th numeric>{t('keyCost')}</Th>
                <Th numeric>{t('colShare')}</Th>
              </Tr>
            </THead>
            <TBody>
              {data.by_model.map((m) => (
                <Tr key={m.model_alias}>
                  <Td className="text-foreground">{modelDisplay(m.model_alias)}</Td>
                  <Td numeric className="text-muted-foreground">
                    {m.requests.toLocaleString()}
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    {m.tokens.toLocaleString()}
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    {fmtUsd(m.cost_usd)}
                  </Td>
                  <Td numeric className="text-muted-foreground">
                    {(m.cost_share * 100).toFixed(1)}%
                  </Td>
                </Tr>
              ))}
              {data.by_model.length === 0 && (
                <Tr>
                  <Td className="text-muted-foreground" colSpan={5}>
                    {t('noUsage')}
                  </Td>
                </Tr>
              )}
            </TBody>
          </Table>
        </div>

        {/* 일별 추이 — 비용/토큰 토글 */}
        <div className="glass rounded-apple p-4">
          <div className="mb-3 flex items-center justify-between">
            <h3 className="text-sm font-semibold">{t('dailyTitle')}</h3>
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
          {data.daily.length > 0 ? (
            <ResponsiveContainer width="100%" height={260}>
              <AreaChart data={data.daily}>
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
                    trendMetric === 'cost_usd' ? fmtUsd(v) : v.toLocaleString()
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
                />
              </AreaChart>
            </ResponsiveContainer>
          ) : (
            <p className="py-16 text-center text-sm text-muted-foreground">
              {t('noUsage')}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}
