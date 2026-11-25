// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 집계 기준 라벨 + 지연 칩(DataFreshness) — wave 2.
 *  - 시각은 브라우저 TZ + TZ 이름(usePointInTime.hmZ), en/ko 문구
 *  - data_as_of 없음 = '실시간 데이터'(롤업을 쓰지 않음)
 *  - delayed_days 가 있으면 amber 칩(라이트/다크 모두 badge-amber 유틸 — globals.css 가 .dark 변형을 준다)
 *  - 서버 렌더에는 시각 텍스트가 없다(하이드레이션 불일치 방지)
 */

import { render, screen } from '@testing-library/react';
import { renderToString } from 'react-dom/server';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

import en from '../../../messages/en.json';
import ko from '../../../messages/ko.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { DataFreshness } from '@/components/common/DataFreshness';
import { fmtHourMinute } from '@/lib/utils/format';
import { tzAbbr } from '@/lib/utils/timezone';

const ISO = '2026-11-24T08:35:00Z';
const BROWSER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;

function wrap(node: React.ReactNode, locale: 'en' | 'ko' = 'en') {
  const messages = (locale === 'en' ? en : ko) as unknown as AbstractIntlMessages;
  return (
    <NextIntlClientProvider locale={locale} messages={messages}>
      <ReportingTimezoneProvider tz="Asia/Kolkata">{node}</ReportingTimezoneProvider>
    </NextIntlClientProvider>
  );
}

const hm = `${fmtHourMinute(ISO, BROWSER_TZ)} ${tzAbbr(BROWSER_TZ, new Date(ISO))}`;

describe('DataFreshness', () => {
  it('en — "As of HH:MM <zone> (today live)" in the browser time zone', () => {
    render(wrap(<DataFreshness meta={{ data_as_of: ISO, delayed_days: [] }} />));
    expect(screen.getByText(`As of ${hm} (today live)`)).toBeTruthy();
    expect(screen.queryByText(/delayed/)).toBeNull();
  });

  it('ko — "집계 기준 HH:MM <zone> · 오늘분 실시간 반영"', () => {
    render(wrap(<DataFreshness meta={{ data_as_of: ISO, delayed_days: [] }} />, 'ko'));
    expect(screen.getByText(`집계 기준 ${hm} · 오늘분 실시간 반영`)).toBeTruthy();
  });

  it('no rollup used → live label', () => {
    render(wrap(<DataFreshness meta={{ data_as_of: null, delayed_days: [] }} />));
    expect(screen.getByText('Live data')).toBeTruthy();
  });

  it('delayed days → amber warning chip with the dates in the tooltip (en/ko)', () => {
    const meta = { data_as_of: ISO, delayed_days: ['2026-11-10', '2026-11-12'] };
    const { unmount } = render(wrap(<DataFreshness meta={meta} />));
    const chip = screen.getByText('2 days delayed');
    expect(chip.getAttribute('title')).toContain('2026-11-10, 2026-11-12');
    expect(chip.closest('.badge')?.className).toContain('badge-amber');
    unmount();
    render(wrap(<DataFreshness meta={{ ...meta, delayed_days: ['2026-11-10'] }} />, 'ko'));
    expect(screen.getByText('1일 집계 지연')).toBeTruthy();
  });

  it('singular in en', () => {
    render(wrap(<DataFreshness meta={{ data_as_of: ISO, delayed_days: ['2026-11-10'] }} />));
    expect(screen.getByText('1 day delayed')).toBeTruthy();
  });

  it('server render has no time text (no pod-TZ output)', () => {
    const html = renderToString(wrap(<DataFreshness meta={{ data_as_of: ISO, delayed_days: [] }} />));
    expect(html).not.toContain('As of');
  });

  it('renders nothing without meta', () => {
    const { container } = render(wrap(<DataFreshness meta={null} />));
    expect(container.textContent).toBe('');
  });

  it('amber badge has a dark-mode variant (light/dark)', () => {
    const css = fs.readFileSync(path.resolve(__dirname, '../../../src/app/globals.css'), 'utf8');
    expect(css).toMatch(/\.badge-amber\s*\{/);
    expect(css).toMatch(/\.dark \.badge-amber\s*\{/);
  });

  it('en and ko define the same freshness keys', () => {
    const k = (m: { common: { freshness: Record<string, string> } }) =>
      Object.keys(m.common.freshness).sort();
    expect(k(en as never)).toEqual(k(ko as never));
  });
});
