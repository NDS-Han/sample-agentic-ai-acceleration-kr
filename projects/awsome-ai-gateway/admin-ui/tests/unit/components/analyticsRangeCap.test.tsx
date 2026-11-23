// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Analytics 직접 입력(custom) 구간 1년 상한 — 날짜 선택기 min/max + 인라인 안내.
 * API(admin-api)는 같은 상한을 422 로 막는다(MAX_RANGE_DAYS 공유 값).
 */

import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import ko from '../../../messages/ko.json';
import { AnalyticsFilter } from '@/components/analytics/AnalyticsFilter';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import type { AnalyticsFilterForm } from '@/types/api';

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
  usePathname: () => '/analytics',
}));

function renderFilter(start: string | null, end: string | null, locale: 'en' | 'ko' = 'en') {
  const filter = {
    period: 'custom',
    start_date: start,
    end_date: end,
    group_by: 'model',
  } as AnalyticsFilterForm;
  return render(
    <NextIntlClientProvider
      locale={locale}
      messages={(locale === 'en' ? en : ko) as unknown as AbstractIntlMessages}
    >
      <ReportingTimezoneProvider tz="America/New_York">
        <AnalyticsFilter defaultValue={filter} periods={[]} currentMonth="2026-11" />
      </ReportingTimezoneProvider>
    </NextIntlClientProvider>,
  );
}

describe('Analytics custom 구간 1년 상한', () => {
  it('선택기 min/max 가 시작/끝 ±365일(양끝 포함 366일)로 묶인다', () => {
    renderFilter('2026-03-01', '2026-11-30');
    const start = screen.getByLabelText('Start Date') as HTMLInputElement;
    const end = screen.getByLabelText('End Date') as HTMLInputElement;
    expect(start.min).toBe('2025-11-30');
    expect(start.max).toBe('2026-11-30');
    expect(end.min).toBe('2026-03-01');
    expect(end.max).toBe('2027-03-01');
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('1년을 넘는 구간(URL 직접 입력 등)은 인라인 안내 — en', () => {
    renderFilter('2025-01-01', '2026-01-02');
    expect(screen.getByRole('alert').textContent).toBe(
      'The date range can be at most 366 days (1 year). Shorten the range.',
    );
    expect((screen.getByLabelText('Start Date') as HTMLInputElement).getAttribute('aria-invalid')).toBe(
      'true',
    );
  });

  it('ko 문구', () => {
    renderFilter('2025-01-01', '2026-01-02', 'ko');
    expect(screen.getByRole('alert').textContent).toBe(
      '기간은 최대 366일(1년)까지 조회할 수 있습니다. 범위를 줄여주세요.',
    );
  });

  it('정확히 366일은 허용', () => {
    renderFilter('2026-01-01', '2027-01-01');
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
