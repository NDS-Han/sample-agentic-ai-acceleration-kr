// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Analytics 지표 카드 — 웹 검색은 모델 비용(Total Cost)과 **별개 카드**.
 * 미추적은 "Not tracked", 일부 미추적이면 그 수를 함께, 구 백엔드(web_search 없음)면
 * 카드를 그리지 않는다(종전 4장 그대로).
 */

import { render, screen } from '@testing-library/react';
import { createTranslator, NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import type { AnalyticsFilterForm } from '@/types/api';
import type { WebSearchUsage } from '@/types/entities';

const fetchAnalyticsOnce = vi.fn();
vi.mock('next-intl/server', () => ({
  getTranslations: async (namespace: string) =>
    createTranslator({ locale: 'en', messages: en as never, namespace: namespace as never }),
}));
vi.mock('@/lib/utils/analyticsQuery', async (orig) => ({
  ...(await orig<typeof import('@/lib/utils/analyticsQuery')>()),
  fetchAnalyticsOnce: (...a: unknown[]) => fetchAnalyticsOnce(...a),
}));

import { ROIMetricsCards } from '@/components/analytics/ROIMetricsCards';

const FILTER = { period: '2026-11', group_by: 'model' } as unknown as AnalyticsFilterForm;

function payload(web_search?: WebSearchUsage) {
  return {
    period: '2026-11',
    currency: 'USD',
    cost_summary: {
      total_requests: 10, error_requests: 0, total_tokens: 1000, total_cost_usd: 2.5,
      active_users: 2, avg_cost_per_user_usd: 1.25,
    },
    by_model: [], by_team: [], trends: [],
    ...(web_search ? { web_search } : {}),
  };
}

async function renderCards() {
  const el = await ROIMetricsCards({ filter: FILTER, latestMonth: '2026-11' });
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz="Asia/Kolkata">{el}</ReportingTimezoneProvider>
    </NextIntlClientProvider>,
  );
}

beforeEach(() => fetchAnalyticsOnce.mockReset());

describe('Analytics — 웹 검색 카드', () => {
  it('모델 비용 카드는 그대로, 웹 검색은 별도 카드 + 무료분 안내', async () => {
    fetchAnalyticsOnce.mockResolvedValue(
      payload({ queries: 12, cost_usd: 0.084, untracked_queries: 0, tracking_since: '2026-11-24T03:00:00Z' }),
    );
    const { container } = await renderCards();
    expect(screen.getByText('$2.50')).toBeTruthy(); // Total Cost = 모델 비용
    expect(screen.getByText('Model cost in USD during the period (excl. web search)')).toBeTruthy();
    expect(screen.getByText('Web search')).toBeTruthy();
    expect(screen.getByText('$0.08')).toBeTruthy();
    expect(screen.getByText('12 queries · not in budgets')).toBeTruthy();
    expect(container.querySelector('[data-testid="web-search-cost-note"]')?.textContent).toContain(
      '5,000 free web search queries per month',
    );
  });

  it('기간 전체가 추적 이전이면 Not tracked — $0 아님', async () => {
    fetchAnalyticsOnce.mockResolvedValue(
      payload({ queries: 7, cost_usd: null, untracked_queries: 7, tracking_since: null }),
    );
    await renderCards();
    expect(screen.getByText('Not tracked')).toBeTruthy();
    expect(screen.queryByText('$0.00')).toBeNull();
  });

  it('일부만 추적됐으면 미추적 검색 수를 함께', async () => {
    fetchAnalyticsOnce.mockResolvedValue(
      payload({ queries: 10, cost_usd: 0.028, untracked_queries: 6, tracking_since: '2026-11-24T03:00:00Z' }),
    );
    await renderCards();
    expect(
      screen.getByText(
        '10 queries · not in budgets · +6 queries before tracking started (cost not tracked)',
      ),
    ).toBeTruthy();
  });

  it('구 백엔드(web_search 없음) — 종전 4장만', async () => {
    fetchAnalyticsOnce.mockResolvedValue(payload());
    const { container } = await renderCards();
    expect(screen.queryByText('Web search')).toBeNull();
    expect(container.querySelector('[data-testid="web-search-cost-note"]')).toBeNull();
  });
});
