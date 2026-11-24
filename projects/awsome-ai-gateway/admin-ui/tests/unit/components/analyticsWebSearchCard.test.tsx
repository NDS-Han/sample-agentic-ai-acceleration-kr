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
  const ws = (o: Partial<WebSearchUsage> = {}): WebSearchUsage => ({
    queries: 12, cost_usd: 0.084, untracked_queries: 0, tracking_since: '2026-11-24T03:00:00Z',
    deployed: true, price_per_1k_queries: '7.00', ...o,
  });

  it('보일 때 — 모델 비용 카드 부제 "Model cost", 별도 웹 검색 카드 + ⓘ 툴팁(보이는 안내 문장 없음)', async () => {
    fetchAnalyticsOnce.mockResolvedValue(payload(ws()));
    const { container } = await renderCards();
    expect(screen.getByText('$2.50')).toBeTruthy();
    expect(screen.getByText('Model cost')).toBeTruthy();
    expect(screen.getByText('Web search')).toBeTruthy();
    expect(screen.getByText('$0.08')).toBeTruthy();
    expect(screen.getByText('12 queries')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'About web search pricing' })).toBeTruthy();
    expect(screen.getByTestId('web-search-price-info').textContent).toContain('($7.00 per 1,000)');
    const visible = container.cloneNode(true) as HTMLElement;
    visible.querySelectorAll('[role="tooltip"]').forEach((n) => n.remove());
    expect(visible.textContent).not.toMatch(/5,000|budget|tracking|since/i);
  });

  it('비용이 기록되지 않은 기간이면 Not tracked — $0 아님', async () => {
    fetchAnalyticsOnce.mockResolvedValue(payload(ws({ queries: 7, cost_usd: null, untracked_queries: 7 })));
    await renderCards();
    expect(screen.getByText('Not tracked')).toBeTruthy();
    expect(screen.queryByText('$0.00')).toBeNull();
  });

  it('일부만 기록됐으면 미기록 검색 수를 함께', async () => {
    fetchAnalyticsOnce.mockResolvedValue(payload(ws({ queries: 10, cost_usd: 0.028, untracked_queries: 6 })));
    await renderCards();
    expect(screen.getByText('10 queries · +6 queries without recorded cost')).toBeTruthy();
  });

  it('미배포·기록 비용 있음 — 배지', async () => {
    fetchAnalyticsOnce.mockResolvedValue(payload(ws({ deployed: false })));
    await renderCards();
    expect(screen.getByText('Web search is not deployed now')).toBeTruthy();
  });

  it.each([
    ['미배포·비용 0', ws({ deployed: false, cost_usd: 0 })],
    ['구 백엔드(web_search 없음)', undefined],
  ])('%s — 종전 4장 그대로', async (_, w) => {
    fetchAnalyticsOnce.mockResolvedValue(payload(w));
    await renderCards();
    expect(screen.queryByText('Web search')).toBeNull();
    expect(screen.getByText('Total USD cost during the period')).toBeTruthy();
  });
});
