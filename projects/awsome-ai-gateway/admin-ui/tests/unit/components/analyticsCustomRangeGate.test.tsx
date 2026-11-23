// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Analytics custom 구간 — 두 날짜가 모두 유효하고 시작<=끝·366일 이내가 아니면
 * 조회하지 않는다(0 지표 금지). 안내는 필터의 날짜 입력 아래 한 곳에만, 툴바 배치는
 * 유효 상태와 같고, 새로고침·내보내기는 비활성.
 */

import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { isValidElement, type ReactElement, type ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import ko from '../../../messages/ko.json';
import { AnalyticsFilter } from '@/components/analytics/AnalyticsFilter';
import { ExportButton } from '@/components/analytics/ExportButton';
import { RefreshButton } from '@/components/analytics/RefreshButton';
import { ROIMetricsCards } from '@/components/analytics/ROIMetricsCards';
import { CostTrendChart } from '@/components/analytics/CostTrendChart';
import { BreakdownChart } from '@/components/analytics/BreakdownChart';
import { TokenAnalysisCard } from '@/components/analytics/TokenAnalysisCard';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { customRangeIssue } from '@/lib/utils/period';
import type { AnalyticsFilterForm } from '@/types/api';

const fetchAnalyticsOnce = vi.fn();
const fetchModelCostAnalytics = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => '/analytics',
}));
vi.mock('next-intl/server', () => ({
  getTranslations: async () => (key: string) => `analytics.${key}`,
}));
vi.mock('@/lib/actions/dashboard', () => ({
  fetchAvailablePeriods: async () => ({ periods: ['2026-11', '2026-10'], latest: '2026-11' }),
}));
vi.mock('@/lib/actions/analytics-models', () => ({
  fetchModelCostAnalytics: (...a: unknown[]) => fetchModelCostAnalytics(...a),
}));
vi.mock('@/lib/utils/analyticsQuery', async (orig) => ({
  ...(await orig<typeof import('@/lib/utils/analyticsQuery')>()),
  fetchAnalyticsOnce: (...a: unknown[]) => fetchAnalyticsOnce(...a),
}));
vi.mock('@/components/analytics/ROIMetricsCards', () => ({ ROIMetricsCards: () => null }));
vi.mock('@/components/analytics/CostTrendChart', () => ({ CostTrendChart: () => null }));
vi.mock('@/components/analytics/BreakdownChart', () => ({ BreakdownChart: () => null }));
vi.mock('@/components/analytics/TokenAnalysisCard', () => ({ TokenAnalysisCard: () => null }));

import AnalyticsPage from '@/app/analytics/page';

/** 렌더하지 않고 요소 트리만 펼친다(서버 컴포넌트 — 자식 데이터 컴포넌트는 그려야 조회한다). */
function walk(node: ReactNode, out: ReactElement[] = []): ReactElement[] {
  if (Array.isArray(node)) node.forEach((n) => walk(n, out));
  else if (isValidElement(node)) {
    out.push(node);
    walk((node.props as { children?: ReactNode }).children, out);
  }
  return out;
}

function texts(node: ReactNode, out: string[] = []): string[] {
  if (typeof node === 'string') out.push(node);
  else if (Array.isArray(node)) node.forEach((n) => texts(n, out));
  else if (isValidElement(node)) texts((node.props as { children?: ReactNode }).children, out);
  return out;
}

async function page(sp: Record<string, string>) {
  const tree = await AnalyticsPage({ searchParams: Promise.resolve(sp) } as never);
  const els = walk(tree);
  const has = (type: unknown) => els.some((e) => e.type === type);
  const named = (name: string) => els.some((e) => typeof e.type === 'function' && e.type.name === name);
  return {
    els,
    text: texts(tree).join(' '),
    dataSections: [ROIMetricsCards, CostTrendChart, BreakdownChart, TokenAnalysisCard].filter(has).length,
    hasContext: named('ContextSection'),
    hasModelCost: named('ModelCostSection'),
    refresh: els.find((e) => e.type === RefreshButton)!.props as { disabled?: boolean },
    exportBtn: els.find((e) => e.type === ExportButton)!.props as { disabled?: boolean },
    empty: els.some((e) => (e.props as Record<string, unknown>)['data-testid'] === 'custom-range-empty'),
  };
}

const BASE = { period: 'custom', group_by: 'model' };
const INVALID: [string, Record<string, string>, string][] = [
  ['끝 날짜만', { ...BASE, end_date: '2026-11-20' }, 'incomplete'],
  ['시작 날짜만', { ...BASE, start_date: '2026-11-01' }, 'incomplete'],
  ['둘 다 없음(직접 입력 막 누름)', { ...BASE }, 'incomplete'],
  ['형식 오류', { ...BASE, start_date: '2026-02-30', end_date: '2026-03-02' }, 'incomplete'],
  ['시작 > 끝', { ...BASE, start_date: '2026-11-20', end_date: '2026-11-01' }, 'reversed'],
  ['366일 초과', { ...BASE, start_date: '2025-01-01', end_date: '2026-11-20' }, 'tooLong'],
];

beforeEach(() => {
  fetchAnalyticsOnce.mockReset();
  fetchModelCostAnalytics.mockReset();
});

describe('customRangeIssue', () => {
  it.each(INVALID)('%s → %s', (_, sp, issue) => {
    expect(customRangeIssue(sp.start_date, sp.end_date)).toBe(issue);
  });
  it('유효 구간(같은 날 포함)·정확히 366일 → null', () => {
    expect(customRangeIssue('2026-08-01', '2026-11-20')).toBeNull();
    expect(customRangeIssue('2026-11-20', '2026-11-20')).toBeNull();
    expect(customRangeIssue('2026-01-01', '2027-01-01')).toBeNull();
  });
});

describe('분석 페이지 — 조회 불가 구간이면 조회하지 않는다', () => {
  it.each(INVALID)('%s: 데이터 섹션·컨텍스트 없음, 빈 상태, 버튼 비활성, 배너 없음', async (_, sp) => {
    const p = await page(sp);
    expect(p.dataSections).toBe(0);
    expect(p.hasContext).toBe(false);
    expect(p.hasModelCost).toBe(false);
    expect(p.empty).toBe(true);
    expect(p.refresh.disabled).toBe(true);
    expect(p.exportBtn.disabled).toBe(true);
    // 이유 문구는 필터에만 — 페이지 본문에 배너로 반복하지 않는다.
    expect(p.text).not.toContain('analytics.invalidDateRange');
    expect(p.text).not.toContain('analytics.rangeTooLong');
    expect(p.text).not.toContain('analytics.selectDateRange');
    expect(fetchAnalyticsOnce).not.toHaveBeenCalled();
    expect(fetchModelCostAnalytics).not.toHaveBeenCalled();
  });

  it('유효 구간은 종전대로 조회 섹션을 그리고 버튼 활성', async () => {
    const p = await page({ ...BASE, start_date: '2026-08-01', end_date: '2026-11-20' });
    expect(p.dataSections).toBe(4);
    expect(p.hasContext).toBe(true);
    expect(p.hasModelCost).toBe(true);
    expect(p.empty).toBe(false);
    expect(p.refresh.disabled).toBe(false);
    expect(p.exportBtn.disabled).toBe(false);
  });

  it('월 모드는 영향 없음', async () => {
    const p = await page({ period: '2026-10', group_by: 'model' });
    expect(p.dataSections).toBe(4);
    expect(p.refresh.disabled).toBe(false);
  });
});

function renderFilter(start: string | null, end: string | null, locale: 'en' | 'ko' = 'en') {
  const filter = { period: 'custom', start_date: start, end_date: end, group_by: 'model' } as AnalyticsFilterForm;
  return render(
    <NextIntlClientProvider locale={locale} messages={(locale === 'en' ? en : ko) as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz="Asia/Kolkata">
        <AnalyticsFilter defaultValue={filter} periods={[]} currentMonth="2026-11" />
      </ReportingTimezoneProvider>
    </NextIntlClientProvider>,
  );
}

describe('필터 — 안내는 한 번, 툴바 배치 불변', () => {
  const CASES: [string, string | null, string | null, string, 'status' | 'alert'][] = [
    ['끝 날짜만', null, '2026-11-20', 'Select a start and end date', 'status'],
    ['시작 날짜만', '2026-11-01', null, 'Select a start and end date', 'status'],
    [
      '시작 > 끝',
      '2026-11-20',
      '2026-11-01',
      'The start date must be on or before the end date. Please adjust the date range.',
      'alert',
    ],
    [
      '366일 초과',
      '2025-01-01',
      '2026-11-20',
      'The date range can be at most 366 days (1 year). Shorten the range.',
      'alert',
    ],
  ];

  it.each(CASES)('%s', (_, start, end, msg, role) => {
    const { container } = renderFilter(start, end);
    const notes = container.querySelectorAll('#custom_range_message');
    expect(notes).toHaveLength(1);
    const note = notes[0] as HTMLElement;
    expect(note.textContent).toBe(msg);
    expect(note.getAttribute('role')).toBe(role);
    // 툴바를 밀어내지 않는다 — 날짜 입력 묶음 안에 absolute, 줄바꿈 강제(basis-full) 없음.
    expect(note.className).toContain('absolute');
    expect(note.className).not.toContain('basis-full');
    expect(note.parentElement?.className).toContain('relative');
    expect(note.parentElement?.contains(screen.getByLabelText('End Date'))).toBe(true);
    // 그룹 기준 선택은 날짜 입력과 같은 줄(형제) 그대로.
    expect(screen.getByLabelText('Group By').parentElement).toBe(note.parentElement?.parentElement);
  });

  it('ko 안내 문구', () => {
    const { container } = renderFilter(null, '2026-11-20', 'ko');
    expect(container.querySelector('#custom_range_message')?.textContent).toBe('시작일과 종료일을 선택하세요');
  });

  it('유효 구간에는 안내 없음', () => {
    const { container } = renderFilter('2026-08-01', '2026-11-20');
    expect(container.querySelector('#custom_range_message')).toBeNull();
  });
});

describe('Refresh/Export 비활성', () => {
  it('disabled prop 이 버튼에 반영된다', () => {
    render(
      <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
        <ReportingTimezoneProvider tz="Asia/Kolkata">
          <RefreshButton disabled />
          <ExportButton filter={{ period: 'custom', group_by: 'model' } as AnalyticsFilterForm} disabled />
        </ReportingTimezoneProvider>
      </NextIntlClientProvider>,
    );
    for (const b of screen.getAllByRole('button')) expect((b as HTMLButtonElement).disabled).toBe(true);
  });
});
