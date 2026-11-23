// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 리포팅 TZ 캡션·시점 포맷터 — 이름은 REPORTING_TIMEZONE(provider)에서 유도하고,
 * 서버 렌더에는 시간대 의존 텍스트를 내지 않는다(파드 TZ 출력·하이드레이션 불일치 방지).
 */

import { render, screen } from '@testing-library/react';
import { renderToString } from 'react-dom/server';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { describe, expect, it } from 'vitest';

import messages from '../../../messages/en.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { BrowserTzCaption, ReportingTzCaption } from '@/components/common/ReportingTzCaption';
import { usePointInTime } from '@/components/common/usePointInTime';
import { tzAbbr } from '@/lib/utils/timezone';
import { fmtDateMinute } from '@/lib/utils/format';

function wrap(node: React.ReactNode, tz = 'Asia/Kolkata') {
  return (
    <NextIntlClientProvider locale="en" messages={messages as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz={tz}>{node}</ReportingTimezoneProvider>
    </NextIntlClientProvider>
  );
}

function Stamp({ iso }: { iso: string }) {
  const pit = usePointInTime();
  return (
    <span>
      <b data-testid="h">{pit.header('Time')}</b>
      <i data-testid="v">{pit.dateTimeZ(iso)}</i>
      <u data-testid="s">{pit.stamp(iso)}</u>
    </span>
  );
}

describe('ReportingTzCaption', () => {
  it('리포팅 TZ 에서 이름을 유도한다 — 하드코딩 없음', () => {
    render(wrap(<ReportingTzCaption kind="dates" />));
    expect(screen.getByText('Dates in IST (Asia/Kolkata)')).toBeTruthy();
  });

  it('dateOnly — 표 안 날짜 열 캡션', () => {
    render(wrap(<ReportingTzCaption kind="dateOnly" />));
    expect(screen.getByText('Dates (YYYY-MM-DD) in IST (Asia/Kolkata)')).toBeTruthy();
  });

  it('kind 별 문구', () => {
    render(wrap(<ReportingTzCaption kind="hours" />, 'UTC'));
    expect(screen.getByText('Hours in UTC')).toBeTruthy();
  });

  it('서버 렌더에는 시간대 텍스트가 없다', () => {
    const html = renderToString(wrap(<ReportingTzCaption kind="period" />));
    expect(html).not.toContain('Asia/Kolkata');
  });
});

describe('usePointInTime', () => {
  it('클라이언트에서는 브라우저 TZ 로 그리고 이름을 붙인다', () => {
    render(wrap(<Stamp iso="2026-11-06T03:43:00Z" />));
    const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    expect(screen.getByTestId('h').textContent).toMatch(/^Time \(.+\)$/);
    expect(screen.getByTestId('v').textContent).not.toBe('');
    // stamp 는 리포팅 TZ(Kolkata)가 아니라 브라우저 TZ 로.
    expect(screen.getByTestId('s').textContent).toBe(
      fmtDateMinute('2026-11-06T03:43:00Z', browserTz),
    );
    expect(browserTz).toBeTruthy();
  });

  it('서버 렌더에는 시각도 TZ 이름도 내지 않는다(파드 TZ 출력 없음)', () => {
    const html = renderToString(wrap(<Stamp iso="2026-11-06T03:43:00Z" />));
    expect(html).toContain('<b data-testid="h">Time</b>');
    expect(html).toMatch(/<i data-testid="v"><\/i>/);
    expect(html).toMatch(/<u data-testid="s"><\/u>/);
  });
});

describe('BrowserTzCaption', () => {
  it('리포팅 TZ 가 아니라 브라우저 TZ 이름을 적는다', () => {
    render(wrap(<BrowserTzCaption />, 'Asia/Kolkata'));
    const browserTz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    expect(screen.getByText(`Times in ${tzAbbr(browserTz)} (your browser)`)).toBeTruthy();
  });

  it('서버 렌더에는 TZ 이름이 없다', () => {
    const html = renderToString(wrap(<BrowserTzCaption />));
    expect(html).not.toContain('Times in');
  });
});
