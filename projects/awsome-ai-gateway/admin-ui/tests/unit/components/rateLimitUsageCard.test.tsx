// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 'Rate limit 사용량' 카드 — 기존 LiveRpmCard + UsageTrendChart 재사용.
 *  - 한도 없음: 최근 60초는 '—' + 미계량 설명, 막대 없음
 *  - 한도 있음: "12 / 60" + 막대(20%), 차트에 적용 한도 기준선
 *  - 구간에 사용 없음: 키 큰 빈 차트 대신 한 줄짜리 빈 상태
 * + Rate limit 대화상자의 최근 24시간 최대 사용량 요약.
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { RateLimitUsageCard } from '@/components/users/detail/RateLimitUsageCard';
import { RateLimitDialog, fmtPeak } from '@/components/users/detail/RateLimitDialog';
import type { RateSummary } from '@/components/users/detail/policyRows';
import { trendPeaks } from '@/lib/utils/rateLimitUsage';

// recharts 는 jsdom 에서 크기 0 — 차트 대신 기준선(한도)과 데이터 존재만 검증한다.
vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div data-testid="chart">{children}</div>,
  AreaChart: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Area: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ReferenceLine: ({ y }: { y: number }) => <div data-testid="limit-line">{y}</div>,
}));
const { rate } = vi.hoisted(() => ({
  rate: { getRateLimitStatusAction: vi.fn(), setRateLimitAction: vi.fn(), deleteRateLimitAction: vi.fn() },
}));
vi.mock('@/lib/actions/rate-limits', () => rate);
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast: vi.fn() }) }));

const NOW = 1_795_500_000;
function stubFetch(usage: object, trendPoints: { requests: number; tokens: number; cost_usd: number }[]) {
  const f = vi.fn(async (input: RequestInfo | URL) => {
    const u = new URL(String(input), 'http://x');
    const body = u.pathname.endsWith('/usage-trend')
      ? { available: true, window_sec: 86400, bucket_sec: 300, points: trendPoints.map((p, i) => ({ t: NOW + i * 300, ...p })) }
      : { available: true, window_sec: 60, by_model: [], ...usage };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', f);
  return f;
}
afterEach(() => vi.unstubAllGlobals());

function wrap(node: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz="Asia/Kolkata">{node}</ReportingTimezoneProvider>
    </NextIntlClientProvider>,
  );
}

const summary = (rpm: number | null, cph: number | null = null): RateSummary => {
  const v = { rpm, tpm: null, cpm: null, cph };
  return { own: rpm == null && cph == null ? null : v, team: null, org: null, source: rpm == null ? 'none' : 'own', effective: v };
};

describe('Rate limit 사용량 카드', () => {
  it('한도 없음 — 최근 60초는 — + 미계량 설명, 막대 없음', async () => {
    const f = stubFetch({ tracked: false, rpm_used_total: 0 }, [{ requests: 10, tokens: 500, cost_usd: 0.01 }]);
    wrap(<RateLimitUsageCard scope="team" id="t1" rate={summary(null)} />);
    const live = await screen.findByTestId('live-rpm');
    expect(within(live).getByText('—')).toBeTruthy();
    expect(within(live).getByText(en.users.rateLimit.untrackedHint)).toBeTruthy();
    expect(within(live).queryByTestId('live-rpm-bar')).toBeNull();
    expect(await screen.findByTestId('chart')).toBeTruthy();
    expect(screen.queryByTestId('limit-line')).toBeNull();
    // 팀 범위로 조회
    expect(f.mock.calls.some((c) => String(c[0]).includes('scope=TEAM') && String(c[0]).includes('scope_id=t1'))).toBe(true);
  });

  it('한도 있음 — 12 / 60 + 막대 20%, 차트에 RPM 한도 기준선', async () => {
    stubFetch({ tracked: true, rpm_used_total: 12 }, [{ requests: 50, tokens: 1000, cost_usd: 0.02 }]);
    wrap(<RateLimitUsageCard scope="user" id="u1" rate={summary(60)} />);
    expect((await screen.findByTestId('live-rpm-value')).textContent).toBe('12 / 60');
    expect((screen.getByTestId('live-rpm-bar').firstChild as HTMLElement).style.width).toBe('20%');
    expect((await screen.findByTestId('limit-line')).textContent).toBe('60');
  });

  it('구간에 사용 없음 — 한 줄 빈 상태(150px 빈 차트 아님)', async () => {
    stubFetch({ tracked: true, rpm_used_total: 0 }, [{ requests: 0, tokens: 0, cost_usd: 0 }]);
    wrap(<RateLimitUsageCard scope="user" id="u1" rate={summary(60)} />);
    const empty = await screen.findByTestId('usage-trend-empty');
    expect(empty.textContent).toBe(en.rateLimits.trendEmpty);
    expect(empty.parentElement?.className).not.toContain('h-[150px]');
    expect(screen.queryByTestId('chart')).toBeNull();
    expect(screen.getByText(en.users.rateLimit.noRecentRequests.replace('{seconds}', '60'))).toBeTruthy();
  });
});

describe('Rate limit 대화상자 — 최근 24시간 최대', () => {
  it('지표별 최대치(분당·시간당 정규화)를 읽기 전용으로', async () => {
    rate.getRateLimitStatusAction.mockResolvedValue({ success: true, data: { own: null, inherited: null, inherited_scope: null } });
    // 5분 버킷: 60 요청 → 12 req/min, 15,000 토큰 → 3,000 tokens/min, $0.05 → $0.01/min · $0.6/hour
    stubFetch({}, [
      { requests: 10, tokens: 1000, cost_usd: 0.01 },
      { requests: 60, tokens: 15_000, cost_usd: 0.05 },
    ]);
    wrap(<RateLimitDialog isOpen onClose={vi.fn()} scope="user" id="u1" target="Kim" team={null} org={null} onSaved={vi.fn()} />);
    expect((await screen.findByTestId('peak-rpm')).textContent).toBe('Peak 24h: 12 req/min');
    expect(screen.getByTestId('peak-tpm').textContent).toBe('Peak 24h: 3,000 tokens/min');
    expect(screen.getByTestId('peak-cpm').textContent).toBe('Peak 24h: $0.01/min');
    expect(screen.getByTestId('peak-cph').textContent).toBe('Peak 24h: $0.6/hour');
  });

  it('24시간 사용이 없으면 "사용 없음"', async () => {
    rate.getRateLimitStatusAction.mockResolvedValue({ success: true, data: { own: null, inherited: null, inherited_scope: null } });
    stubFetch({}, []);
    wrap(<RateLimitDialog isOpen onClose={vi.fn()} scope="team" id="t1" target="Dev" org={null} onSaved={vi.fn()} />);
    await waitFor(() => expect(screen.getByTestId('peak-rpm').textContent).toBe('No usage in the last 24h'));
  });

  it('trendPeaks · fmtPeak', () => {
    expect(trendPeaks({ available: false, window_sec: 0, bucket_sec: 60, points: [] })).toBeNull();
    expect(trendPeaks({ available: true, window_sec: 3600, bucket_sec: 60, points: [{ t: 0, requests: 7, tokens: 9, cost_usd: 0.5 }] })).toEqual({
      rpm: 7,
      tpm: 9,
      cpm: 0.5,
      cph: 30,
    });
    expect(fmtPeak('rpm', 11.2)).toBe('12');
    expect(fmtPeak('cph', 30)).toBe('$30.00');
    expect(fmtPeak('cpm', 0.00123)).toBe('$0.0012');
  });
});
