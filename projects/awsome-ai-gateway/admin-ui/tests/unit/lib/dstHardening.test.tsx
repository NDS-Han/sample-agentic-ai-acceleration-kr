// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * DST 리전(US/EU) 배포 + custom 구간 1년 상한.
 *
 * 각 케이스는 **프로세스 TZ(=브라우저 Intl 기본 TZ)와 REPORTING_TIMEZONE 을 같은 존으로**
 * 두고 돈다 — 날짜 채움이 현지 자정 + 24h 산술이면 이 조합에서 전환일에 날짜가
 * 건너뛰거나 겹친다. 2026 전환일: US 03-08·11-01, EU 03-29·10-25.
 * Asia/Kolkata·Asia/Seoul 은 종전 동작 그대로(회귀).
 */

import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';

// 브라우저 TZ = 프로세스 TZ. 프로덕션 browserTz() 는 모듈 수준에서 한 번만 읽어
// 캐시하므로(실제 브라우저에선 TZ 가 안 바뀐다) 존별로 바꿔 돌리려면 여기서 위임한다.
vi.mock('@/components/common/ReportingTimezoneProvider', async (orig) => {
  const mod = await orig<typeof import('@/components/common/ReportingTimezoneProvider')>();
  return {
    ...mod,
    useDisplayTz: () => (mod.useIsClient() ? Intl.DateTimeFormat().resolvedOptions().timeZone : null),
  };
});

import messages from '../../../messages/en.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { BrowserTzCaption, ReportingTzCaption } from '@/components/common/ReportingTzCaption';
import { usePointInTime } from '@/components/common/usePointInTime';
import { fmtTick, fmtTooltipTime } from '@/components/rate-limits/UsageTrendChart';
import {
  addDaysIso,
  isRangeTooLong,
  MAX_RANGE_DAYS,
  rangeDaysInclusive,
} from '@/lib/utils/period';
import { tzExact, tzHasDst, tzZoneLabel, tzZoneText } from '@/lib/utils/timezone';
import { buildTrendSeries, fillDateGaps } from '@/lib/utils/trendSeries';

const ORIGINAL_TZ = process.env.TZ;
const ORIGINAL_RTZ = process.env.REPORTING_TIMEZONE;
afterAll(() => {
  process.env.TZ = ORIGINAL_TZ;
  process.env.REPORTING_TIMEZONE = ORIGINAL_RTZ;
});

function useZone(tz: string) {
  beforeEach(() => {
    process.env.TZ = tz;
    process.env.REPORTING_TIMEZONE = tz;
  });
}

const DST_ZONES = [
  { tz: 'America/Los_Angeles', spring: '2026-03-08', fall: '2026-11-01', label: 'PT' },
  { tz: 'America/New_York', spring: '2026-03-08', fall: '2026-11-01', label: 'ET' },
  { tz: 'Europe/Berlin', spring: '2026-03-29', fall: '2026-10-25', label: 'Europe/Berlin' },
  { tz: 'Europe/London', spring: '2026-03-29', fall: '2026-10-25', label: 'Europe/London' },
] as const;

const NO_DST_ZONES = [
  { tz: 'Asia/Kolkata', label: 'IST' },
  { tz: 'Asia/Seoul', label: 'GMT+9' },
] as const;

function expectConsecutive(days: string[]) {
  expect(new Set(days).size).toBe(days.length); // 중복 없음
  for (let i = 1; i < days.length; i++) {
    const prev = new Date(`${days[i - 1]}T00:00:00Z`);
    prev.setUTCDate(prev.getUTCDate() + 1);
    expect(days[i]).toBe(prev.toISOString().slice(0, 10)); // 건너뜀 없음
  }
}

function wrap(node: React.ReactNode, tz: string) {
  return (
    <NextIntlClientProvider locale="en" messages={messages as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz={tz}>{node}</ReportingTimezoneProvider>
    </NextIntlClientProvider>
  );
}

function Header({ rows }: { rows: string[] }) {
  const pit = usePointInTime();
  return (
    <div>
      <b data-testid="h">{pit.header('Time')}</b>
      {rows.map((iso) => (
        <i key={iso} data-testid="cell" title={pit.cellTitle(iso)}>
          {pit.dateTime(iso)}
        </i>
      ))}
    </div>
  );
}

describe.each([...DST_ZONES, ...NO_DST_ZONES.map((z) => ({ ...z, spring: '2026-03-08', fall: '2026-11-01' }))])(
  'B2 날짜 채움 — $tz',
  ({ tz, spring, fall }) => {
    useZone(tz);

    it('전환일을 낀 구간: 빠진 날 없이, 겹치는 날 없이', () => {
      for (const d of [spring, fall]) {
        const before = addDaysIso(d, -3)!;
        const after = addDaysIso(d, 3)!;
        const filled = fillDateGaps([before, after]);
        expect(filled).toHaveLength(7);
        expect(filled[3]).toBe(d);
        expectConsecutive(filled);
      }
    });

    it('buildTrendSeries 의 range 확장도 달력 날짜 단위 — 3~11월 전체', () => {
      const s = buildTrendSeries([{ date: '2026-03-10', cost_usd: 1 }], [], undefined, undefined, {
        range: { start: '2026-03-01', end: '2026-11-30' },
        today: '2026-11-30',
      });
      expect(s.dates[0]).toBe('2026-03-01');
      expect(s.dates.at(-1)).toBe('2026-11-30');
      expect(s.dates).toHaveLength(275);
      expectConsecutive(s.dates);
      expect(s.dates).toContain(spring);
      expect(s.dates).toContain(fall);
    });

    it('addDaysIso 는 전환일에도 하루씩', () => {
      expect(addDaysIso(spring, 1)).toBe(
        new Date(Date.parse(`${spring}T00:00:00Z`) + 86_400_000).toISOString().slice(0, 10),
      );
      expect(addDaysIso(addDaysIso(fall, -1)!, 2)).toBe(addDaysIso(fall, 1));
    });
  },
);

describe('A — 구간 1년 상한(MAX_RANGE_DAYS)', () => {
  it('공유 상수 = 366 (admin-api usage_filters.MAX_RANGE_DAYS 와 같은 값)', () => {
    expect(MAX_RANGE_DAYS).toBe(366);
  });

  it('366일(양끝 포함)까지 허용, 367일부터 초과', () => {
    expect(rangeDaysInclusive('2026-01-01', '2027-01-01')).toBe(366);
    expect(isRangeTooLong('2026-01-01', '2027-01-01')).toBe(false);
    expect(isRangeTooLong('2026-01-01', '2027-01-02')).toBe(true);
    expect(isRangeTooLong('2028-01-01', '2028-12-31')).toBe(false); // 윤년 366일
    expect(isRangeTooLong('2026-01-01', null)).toBe(false);
    expect(isRangeTooLong('bad', '2027-01-02')).toBe(false);
  });

  it('trendSeries 의 날짜 채움 가드도 같은 상한 — 366일은 채우고 367일은 그대로', () => {
    expect(fillDateGaps(['2026-01-01', '2027-01-01'])).toHaveLength(366);
    expect(fillDateGaps(['2026-01-01', '2027-01-02'])).toEqual(['2026-01-01', '2027-01-02']);
  });

  it('선택기 min/max 계산 — 시작 + 365일이 마지막 허용일', () => {
    expect(addDaysIso('2026-01-01', MAX_RANGE_DAYS - 1)).toBe('2027-01-01');
    expect(addDaysIso('2027-01-01', -(MAX_RANGE_DAYS - 1))).toBe('2026-01-01');
  });
});

describe.each(DST_ZONES)('B3/B4 DST 독립 이름 — $tz', ({ tz, spring, fall, label }) => {
  useZone(tz);

  it('DST 존으로 판정되고, 전환 전후 같은 이름', () => {
    expect(tzHasDst(tz)).toBe(true);
    for (const d of [spring, fall]) {
      const before = new Date(`${addDaysIso(d, -1)}T12:00:00Z`);
      const after = new Date(`${addDaysIso(d, 1)}T12:00:00Z`);
      expect(tzZoneLabel(tz, before)).toBe(label);
      expect(tzZoneLabel(tz, after)).toBe(label);
    }
  });

  it('열 제목은 DST 독립, 셀 툴팁은 행마다 정확한 약어·오프셋', () => {
    const summer = `${addDaysIso(fall, -10)}T12:00:00Z`;
    const winter = `${addDaysIso(fall, 10)}T12:00:00Z`;
    render(wrap(<Header rows={[summer, winter]} />, tz));
    expect(screen.getByTestId('h').textContent).toBe(`Time (${label})`);
    const [a, b] = screen.getAllByTestId('cell').map((c) => c.getAttribute('title') ?? '');
    expect(a).not.toBe(b);
    expect(a).toContain(tzExact(tz, new Date(summer)));
    expect(b).toContain(tzExact(tz, new Date(winter)));
    expect(a).toContain(tz);
  });

  it('리포팅 TZ 캡션에 EST/EDT 같은 계절 약어가 없다', () => {
    render(wrap(<ReportingTzCaption kind="dates" />, tz));
    const text = screen.getByText(/^Dates in /).textContent ?? '';
    expect(text).toBe(label === tz ? `Dates in ${tz}` : `Dates in ${label} (${tz})`);
    expect(text).not.toMatch(/\b(PST|PDT|EST|EDT|CET|CEST|BST)\b/);
  });

  it('브라우저 TZ 캡션도 같은 DST 독립 이름', () => {
    render(wrap(<BrowserTzCaption />, tz));
    expect(screen.getByText(`Times in ${label} (your browser)`)).toBeTruthy();
  });
});

describe('B3/B4 행별 정확한 이름(tzExact)', () => {
  it('US: PDT/PST + 오프셋', () => {
    expect(tzExact('America/Los_Angeles', new Date('2026-11-01T08:30:00Z'))).toBe('PDT (GMT-7)');
    expect(tzExact('America/Los_Angeles', new Date('2026-11-01T09:30:00Z'))).toBe('PST (GMT-8)');
    expect(tzExact('America/New_York', new Date('2026-03-08T06:30:00Z'))).toBe('EST (GMT-5)');
    expect(tzExact('America/New_York', new Date('2026-03-08T07:30:00Z'))).toBe('EDT (GMT-4)');
  });

  it('EU: 계절 약어 또는 오프셋', () => {
    expect(tzExact('Europe/Berlin', new Date('2026-10-25T00:30:00Z'))).toBe('CEST (GMT+2)');
    expect(tzExact('Europe/Berlin', new Date('2026-10-25T01:30:00Z'))).toBe('CET (GMT+1)');
    expect(tzExact('Europe/London', new Date('2026-10-25T00:30:00Z'))).toBe('BST (GMT+1)');
    expect(tzExact('Europe/London', new Date('2026-10-25T01:30:00Z'))).toBe('GMT');
  });
});

describe.each(NO_DST_ZONES)('회귀 — DST 없는 존 $tz 는 종전 그대로', ({ tz, label }) => {
  useZone(tz);

  it('이름·캡션 불변', () => {
    expect(tzHasDst(tz)).toBe(false);
    expect(tzZoneLabel(tz)).toBe(label);
    expect(tzZoneText(tz)).toBe(`${label} (${tz})`);
    render(wrap(<Header rows={['2026-11-01T09:30:00Z']} />, tz));
    expect(screen.getByTestId('h').textContent).toBe(`Time (${label})`);
  });

  it('툴팁에 오프셋을 덧붙이지 않는다(축 라벨과 동일)', () => {
    const t = Date.UTC(2026, 10, 1, 9, 0) / 1000;
    expect(fmtTooltipTime(t, 3600, tz, 'en')).toBe(fmtTick(t, 3600, tz, 'en'));
  });
});

describe('B5 시간 차트 — 가을 전환일에 반복되는 현지 시각', () => {
  // 같은 현지 시각(벽시계)이 두 번 오는 두 UTC 정시.
  const CASES = [
    { tz: 'America/Los_Angeles', a: '2026-11-01T08:00:00Z', b: '2026-11-01T09:00:00Z' }, // 01:00 ×2
    { tz: 'America/New_York', a: '2026-11-01T05:00:00Z', b: '2026-11-01T06:00:00Z' }, // 01:00 ×2
    { tz: 'Europe/Berlin', a: '2026-10-25T00:00:00Z', b: '2026-10-25T01:00:00Z' }, // 02:00 ×2
    { tz: 'Europe/London', a: '2026-10-25T00:00:00Z', b: '2026-10-25T01:00:00Z' }, // 01:00 ×2
  ] as const;

  it.each(CASES)('$tz: 축 라벨은 같아도 툴팁은 오프셋으로 구분된다', ({ tz, a, b }) => {
    process.env.TZ = tz;
    const ta = Date.parse(a) / 1000;
    const tb = Date.parse(b) / 1000;
    expect(fmtTick(ta, 3600, tz, 'en')).toBe(fmtTick(tb, 3600, tz, 'en'));
    const la = fmtTooltipTime(ta, 3600, tz, 'en');
    const lb = fmtTooltipTime(tb, 3600, tz, 'en');
    expect(la).not.toBe(lb);
    expect(la).toMatch(/ GMT[+-]\d+$/);
    // 분 단위 버킷(1h 창)도 마찬가지
    expect(fmtTooltipTime(ta, 60, tz, 'en')).not.toBe(fmtTooltipTime(tb, 60, tz, 'en'));
    // 브라우저 TZ 를 못 받은 경우(런타임 TZ 폴백)도 같은 결과
    expect(fmtTooltipTime(ta, 3600, undefined, 'en')).toBe(la);
  });
});
