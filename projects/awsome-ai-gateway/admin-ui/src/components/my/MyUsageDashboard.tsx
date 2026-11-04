'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useTranslations } from 'next-intl';
import type { MyUsageResponse } from '@/lib/actions/my';
import { fmtUsd } from '@/lib/utils/format';
import { modelDisplay } from '@/lib/utils/modelLabel';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { TokenMixDonutClient } from '@/components/analytics/TokenMixDonutClient';

export function MyUsageDashboard({ data }: { data: MyUsageResponse }) {
  const t = useTranslations('my');
  // summary 는 서버 단일 집계 — 일별 합산과 같은 §59 스코프라 카드 값이 정확하다.
  const totalCost = data.summary?.cost_usd ?? data.daily_usage.reduce((sum, d) => sum + d.cost_usd, 0);
  const totalRequests = data.summary?.requests ?? data.daily_usage.reduce((sum, d) => sum + d.requests, 0);
  const s = data.summary;
  const totalTokens = s
    ? s.input_tokens + s.output_tokens + s.cache_creation_tokens + s.cache_read_tokens
    : data.daily_usage.reduce((sum, d) => sum + d.tokens, 0);

  return (
    <div className="space-y-6">
      {/* Summary Cards */}
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-4">
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">{t('totalCost')}</p>
          <p className="text-2xl font-bold mt-1">{fmtUsd(totalCost)}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">{t('totalRequests')}</p>
          <p className="text-2xl font-bold mt-1">{totalRequests.toLocaleString()}</p>
        </div>
        <div className="glass glass-hover rounded-apple p-4">
          <p className="text-sm text-muted-foreground">{t('totalTokens')}</p>
          <p className="text-2xl font-bold mt-1">{totalTokens.toLocaleString()}</p>
        </div>
      </div>

      {/* Daily Usage Table */}
      <div className="glass rounded-apple overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-semibold">{t('dailyUsage')}</h3>
        </div>
        {data.daily_usage.length === 0 ? (
          <p className="text-sm text-muted-foreground p-4">{t('noDailyRecords')}</p>
        ) : (
          <Table>
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
        )}
      </div>

      {/* By Model Table */}
      <div className="glass rounded-apple overflow-hidden">
        <div className="px-4 py-3 border-b border-border">
          <h3 className="text-sm font-semibold tracking-tight">{t('modelUsage')}</h3>
        </div>
        {data.by_model.length === 0 ? (
          <p className="text-sm text-muted-foreground p-4">{t('noModelRecords')}</p>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>{t('colModel')}</Th>
                <Th numeric>{t('colCostUsd')}</Th>
                <Th numeric>{t('colRequests')}</Th>
                <Th numeric>{t('colTokens')}</Th>
              </Tr>
            </THead>
            <TBody>
              {data.by_model.map((row) => (
                <Tr key={row.model_alias}>
                  <Td emphasis className="text-xs" title={row.model_alias}>
                    {modelDisplay(row.model_alias, row.display_name)}
                  </Td>
                  <Td numeric>{fmtUsd(row.cost_usd)}</Td>
                  <Td numeric>{row.requests.toLocaleString()}</Td>
                  <Td numeric>{row.tokens.toLocaleString()}</Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>

      {/* 토큰 분석 — 버킷 구성 비율 + 비청구 reasoning. 에이전트 상세·analytics 와 동일 컴포넌트. */}
      {s && (
        <div className="glass rounded-apple p-4">
          <h3 className="mb-3 text-sm font-semibold">{t('tokTitle')}</h3>
          {totalTokens > 0 ? (
            <TokenMixDonutClient
              data={{
                input_tokens: s.input_tokens,
                output_tokens: s.output_tokens,
                cache_read_tokens: s.cache_read_tokens,
                cache_write_tokens: s.cache_creation_tokens,
                total_tokens: totalTokens,
              }}
            />
          ) : (
            <p className="py-4 text-center text-sm text-muted-foreground">
              {t('noDailyRecords')}
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
      )}
    </div>
  );
}
