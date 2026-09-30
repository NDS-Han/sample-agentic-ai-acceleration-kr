'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useTranslations } from 'next-intl';
import type { ModelCostAnalyticsResponse } from '@/lib/actions/analytics-models';
import { fmtPricePerM } from '@/lib/utils/pricing';
import { fmtUsd } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

// 비용 비중 바 팔레트 — 라이트/다크 모두에서 표면 대비가 확보되는 채도군.
// 색상 순서는 모델 정렬 순(비용 내림차순)에 매핑.
const SHARE_COLORS = [
  'bg-[#2dd4bf]',
  'bg-[#38bdf8]',
  'bg-[#f472b6]',
  'bg-[#a78bfa]',
  'bg-[#fbbf24]',
  'bg-[#4ade80]',
  'bg-[#fb7185]',
  'bg-[#818cf8]',
];

export function ModelCostDetail({ data }: { data: ModelCostAnalyticsResponse }) {
  const t = useTranslations('analyticsModels');
  const models = data.models;

  return (
    <div className="space-y-6">
      {/* Summary */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">{t('totalCost')}</p>
          <p className="text-2xl font-bold mt-1 tracking-tight num">{fmtUsd(data.total_cost_usd)}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">{t('activeModels')}</p>
          <p className="text-2xl font-bold mt-1">{models.length}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">{t('period')}</p>
          <p className="text-2xl font-bold mt-1">{data.period}</p>
        </div>
      </div>

      {/* Cost Breakdown Bar */}
      {models.length > 0 && data.total_cost_usd > 0 && (
        <div className="glass glass-hover rounded-apple p-4">
          <h3 className="text-sm font-semibold mb-3">{t('costShare')}</h3>
          <div
            className="flex h-6 rounded-full overflow-hidden bg-muted"
            role="img"
            aria-label={t('costShareAria')}
          >
            {models.map((m, i) => {
              const pct = (m.total_cost_usd / data.total_cost_usd) * 100;
              if (pct < 1) return null;
              return (
                <div
                  key={m.model_alias}
                  className={`${SHARE_COLORS[i % SHARE_COLORS.length]} relative group`}
                  style={{ width: `${pct}%` }}
                  title={`${m.model_alias}: ${pct.toFixed(1)}%`}
                />
              );
            })}
          </div>
          <div className="flex flex-wrap gap-3 mt-2">
            {models.map((m, i) => {
              const pct = (m.total_cost_usd / data.total_cost_usd) * 100;
              return (
                <div key={m.model_alias} className="flex items-center gap-1.5 text-xs">
                  <span
                    className={`w-2.5 h-2.5 rounded-full ${SHARE_COLORS[i % SHARE_COLORS.length]}`}
                  />
                  <span>
                    {m.model_alias} ({pct.toFixed(1)}%)
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Model Table */}
      <div className="glass rounded-apple overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-semibold">{t('detailTitle')}</h3>
        </div>
        {models.length === 0 ? (
          <p className="text-sm text-muted-foreground p-4">{t('empty')}</p>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>{t('colModel')}</Th>
                <Th numeric>{t('colRequests')}</Th>
                <Th numeric>{t('colCost')}</Th>
                <Th numeric>{t('colInputTokens')}</Th>
                <Th numeric>{t('colOutputTokens')}</Th>
                <Th numeric>{t('colPer1M')}</Th>
                <Th numeric>{t('colAvgLatency')}</Th>
              </Tr>
            </THead>
            <TBody>
              {models.map((m) => (
                <Tr key={m.model_alias}>
                  <Td emphasis className="font-mono mono-id text-xs">{m.model_alias}</Td>
                  <Td numeric>{m.request_count.toLocaleString()}</Td>
                  <Td numeric className="font-semibold">{fmtUsd(m.total_cost_usd)}</Td>
                  <Td numeric>{m.input_tokens.toLocaleString()}</Td>
                  <Td numeric>{m.output_tokens.toLocaleString()}</Td>
                  <Td numeric>{fmtPricePerM(m.cost_per_1k_tokens)}</Td>
                  <Td numeric>{m.avg_latency_ms}ms</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>
    </div>
  );
}
