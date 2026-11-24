'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import { fmtUsd, usdTitle } from '@/lib/utils/format';
import { WebSearchCostNote } from '@/components/common/WebSearchCost';
import { usePointInTime } from '@/components/common/usePointInTime';
import type { MonitoringOverviewResponse } from '@/lib/actions/monitoring';

function StatCard({
  label,
  value,
  valueTitle,
  sub,
}: {
  label: string;
  value: string;
  valueTitle?: string;
  sub?: string;
}) {
  return (
    <div className="glass glass-hover rounded-apple p-4">
      <p className="text-sm text-muted-foreground">{label}</p>
      <p className="text-2xl font-bold mt-1" title={valueTitle}>{value}</p>
      {sub && <p className="text-xs text-muted-foreground mt-1">{sub}</p>}
    </div>
  );
}

export function MonitoringOverview({ data }: { data: MonitoringOverviewResponse }) {
  const t = useTranslations('monitoring');
  const tw = useTranslations('webSearchCost');
  const pit = usePointInTime();
  const h = data.last_1h;
  const ws = h.web_search;

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('lastHourSummary')}</h2>
        {/* 한 시점이라 보는 사람의 브라우저 TZ 로 그린다(리포팅 TZ 는 집계 경계용).
            TZ 이름을 같이 적어 어느 시계 기준인지 분명히 한다. */}
        <span className="text-xs text-muted-foreground" title={pit.title}>
          {/* 마운트 전에는 비워 둔다 — 파드 TZ 로 그린 시각이 잠깐 보이지 않게. */}
          {pit.tz ? t('asOf', { time: pit.dateTime(data.timestamp), tz: pit.zoneAt(data.timestamp) }) : '\u00a0'}
        </span>
      </div>
      <div
        className={`grid grid-cols-2 sm:grid-cols-3 gap-4 ${ws ? 'lg:grid-cols-4 xl:grid-cols-7' : 'lg:grid-cols-6'}`}
      >
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
        <StatCard
          label={t('totalCost')}
          // 단일 값 카드 — 1센트 미만도 유효숫자 2개($0.0071).
          value={fmtUsd(h.total_cost_usd, { subCent: 'significant' })}
          valueTitle={usdTitle(h.total_cost_usd)}
          sub={ws ? tw('modelCost') : undefined}
        />
        {ws && (
          // 웹 검색 — 모델 비용 카드와 별개(예산 제외). null = 미추적, $0 이 아니다.
          <StatCard
            label={tw('title')}
            value={
              ws.cost_usd == null ? tw('notTracked') : fmtUsd(ws.cost_usd, { subCent: 'significant' })
            }
            valueTitle={
              ws.cost_usd == null ? tw('notTrackedHint', { since: 'none' }) : usdTitle(ws.cost_usd)
            }
            sub={
              ws.untracked_queries > 0 && ws.cost_usd != null
                ? `${tw('queries', { count: ws.queries })} · ${tw('untrackedPart', { count: ws.untracked_queries })}`
                : tw('queries', { count: ws.queries })
            }
          />
        )}
        <StatCard label={t('activeModels')} value={String(data.active_models)} />
      </div>
      {ws && <WebSearchCostNote ws={ws} inline />}
    </div>
  );
}