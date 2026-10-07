'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useTranslations } from 'next-intl';
import type { ModelCostAnalyticsResponse } from '@/lib/actions/analytics-models';
import { fmtPricePerM } from '@/lib/utils/pricing';
import { fmtUsd } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';
import { modelDisplay } from '@/lib/utils/modelLabel';

// 모델 색상의 단일 출처는 CATEGORICAL_PALETTE — 같은 페이지의 '모델별 비용'
// 막대 차트(BreakdownChartClient)와 대시보드 도넛이 같은 팔레트를 쓴다.
// 로컬 배열을 따로 두면 같은 모델이 위젯마다 다른 색으로 칠해진다.

export function ModelCostDetail({ data }: { data: ModelCostAnalyticsResponse }) {
  const t = useTranslations('analyticsModels');
  const models = data.models;

  return (
    <div className="space-y-6">
      {/* 섹션 헤더 — 별도 페이지의 요약 카드(총 비용/기간)는 위 ROI 카드·필터바와
          중복이라 제거하고, 여기엔 모델 수·합계만 한 줄로 둔다. */}
      <div className="flex items-baseline justify-between flex-wrap gap-2">
        <h2 className="text-lg font-semibold">{t('detailTitle')}</h2>
        <span className="text-xs text-muted-foreground">
          {t('summaryMeta', { count: models.length, cost: fmtUsd(data.total_cost_usd) })}
        </span>
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
                  className="relative group"
                  style={{
                    width: `${pct}%`,
                    backgroundColor: CATEGORICAL_PALETTE[i % CATEGORICAL_PALETTE.length],
                  }}
                  title={`${modelDisplay(m.model_alias, m.display_name)}: ${pct.toFixed(1)}%`}
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
                    className="w-2.5 h-2.5 rounded-full"
                    style={{
                      backgroundColor: CATEGORICAL_PALETTE[i % CATEGORICAL_PALETTE.length],
                    }}
                  />
                  <span>
                    {modelDisplay(m.model_alias, m.display_name)} ({pct.toFixed(1)}%)
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Model Table */}
      <div className="glass rounded-apple overflow-hidden">
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
                <Th numeric>{t('colCacheTokens')}</Th>
                <Th numeric>{t('colPer1M')}</Th>
                <Th numeric>{t('colAvgLatency')}</Th>
              </Tr>
            </THead>
            <TBody>
              {models.map((m) => {
                const cacheTokens =
                  (m.cache_creation_tokens ?? 0) + (m.cache_read_tokens ?? 0);
                return (
                  <Tr key={m.model_alias}>
                    {/* 대시보드와 같은 표기 규칙 — 표시명 우선, 원시 alias 는 title 로. */}
                    <Td emphasis className="text-xs" title={m.model_alias}>
                      {modelDisplay(m.model_alias, m.display_name)}
                    </Td>
                    <Td numeric>{m.request_count.toLocaleString()}</Td>
                    <Td numeric className="font-semibold">{fmtUsd(m.total_cost_usd)}</Td>
                    <Td numeric>{m.input_tokens.toLocaleString()}</Td>
                    <Td numeric>{m.output_tokens.toLocaleString()}</Td>
                    <Td
                      numeric
                      title={t('cacheSplit', {
                        write: (m.cache_creation_tokens ?? 0).toLocaleString(),
                        read: (m.cache_read_tokens ?? 0).toLocaleString(),
                      })}
                    >
                      {cacheTokens.toLocaleString()}
                    </Td>
                    <Td numeric>{fmtPricePerM(m.cost_per_1k_tokens)}</Td>
                    {/* 백엔드는 avg() NULL 을 0으로 내린다 — '0ms' 는 지연이 0 이
                        아니라 데이터 없음이므로 대시(-)로 표시한다. */}
                    <Td numeric>{m.avg_latency_ms > 0 ? `${m.avg_latency_ms}ms` : '—'}</Td>
                  </Tr>
                );
              })}
            </TBody>
          </Table>
        )}
        {/* 실효 단가의 분모 안내 — input/output 만 합하면 API 계산(캐시 포함)과
            안 맞아 단가가 부풀어 보인다(리뷰 TOP-5). */}
        {models.length > 0 && (
          <p className="text-xs text-muted-foreground px-4 pb-3">{t('per1MHint')}</p>
        )}
      </div>
    </div>
  );
}
