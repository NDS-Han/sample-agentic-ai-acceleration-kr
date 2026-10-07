'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useLocale, useTranslations } from 'next-intl';
import { fmtDateTime, fmtUsd } from '@/lib/utils/format';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import type { MonitoringOverviewResponse } from '@/lib/actions/monitoring';

function StatCard({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div className="glass glass-hover rounded-apple p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold mt-1">{value}</p>
      {sub && <p className="text-xs text-muted-foreground mt-1">{sub}</p>}
    </div>
  );
}

export function MonitoringOverview({ data }: { data: MonitoringOverviewResponse }) {
  const t = useTranslations('monitoring');
  const locale = useLocale();
  const tz = useReportingTz();
  const h = data.last_1h;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('lastHourSummary')}</h2>
        <span className="text-xs text-muted-foreground">
          {fmtDateTime(data.timestamp, locale, tz)}
        </span>
      </div>
      <div className="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-6 gap-4">
        <StatCard label={t('totalRequests')} value={h.total_requests.toLocaleString()} />
        <StatCard
          label={t('errors')}
          value={h.error_count.toLocaleString()}
          sub={`${h.error_rate_pct}%`}
        />
        {/* 요청 0건일 때 0ms 는 실측이 아니라 빈 값 — — 로 표시해 오독 방지 */}
        <StatCard
          label={t('avgLatency')}
          value={h.total_requests > 0 ? `${h.avg_latency_ms}ms` : '—'}
        />
        <StatCard
          label={t('p95Latency')}
          value={h.total_requests > 0 ? `${h.p95_latency_ms}ms` : '—'}
        />
        <StatCard label={t('totalCost')} value={fmtUsd(h.total_cost_usd)} />
        <StatCard label={t('activeModels')} value={String(data.active_models)} />
      </div>
    </div>
  );
}