// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 대시보드 웹 검색 레이아웃(승인안).
 *
 * 표시 규칙 4가지:
 *  A) 배포됨·사용 있음       → 섹션 + 합산 카드
 *  B) 배포됨·검색 0          → 섹션(0 / $0.00) + 합산 카드(웹 검색 $0.00)
 *  C) 미배포·기록 비용 있음   → 섹션 + "현재 미배포" 배지 + 합산 카드
 *  D) 미배포·기록 비용 없음   → 섹션 없음, COST & BUDGET 4장은 기능 이전과 똑같이
 * 그리고 예산 사용률은 어느 경우에도 모델 비용만(웹 검색 무시).
 */

import { render, screen, within } from '@testing-library/react';
import { createTranslator, NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { CostBudgetCards } from '@/components/dashboard/CostBudgetCards';
import { WebSearchSection } from '@/components/dashboard/WebSearchSection';
import type { DashboardKPI, DashboardWebSearch } from '@/lib/actions/dashboard';
import { computeDailyAvg, costBudgetView } from '@/lib/utils/dashboardCost';
import { buildDailyQuerySeries } from '@/lib/utils/webSearchDaily';
import type { WebSearchUsage } from '@/types/entities';

vi.mock('next-intl/server', () => ({
  getTranslations: async (namespace: string) =>
    createTranslator({ locale: 'en', messages: en as never, namespace: namespace as never }),
}));
// recharts 는 jsdom 에서 크기 0 — 차트 자체가 아니라 데이터·텍스트를 검증한다.
vi.mock('recharts', () => ({
  PieChart: () => null, Pie: () => null, Cell: () => null,
  ResponsiveContainer: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  BarChart: ({ data }: { data: unknown[] }) => <div data-testid="bar-data">{JSON.stringify(data)}</div>,
  Bar: () => null, XAxis: () => null, YAxis: () => null, Tooltip: () => null,
}));

const NOW = { y: 2026, m: 11, d: 20 }; // 리포팅 TZ 오늘 = 2026-11-20 (30일 중 20일 경과)

function kpi(ws?: Partial<WebSearchUsage>): DashboardKPI {
  return {
    period: '2026-11', total_requests: 480, error_requests: 0, total_tokens: 1000,
    total_cost_usd: 12.0, active_users: 4, cost_per_user_usd: 3.0,
    budget_used_usd: 50, budget_limit_usd: 100, budget_utilization_pct: 50,
    active_keys: 1, active_models: 1,
    web_search: ws === undefined ? undefined : {
      queries: 96, cost_usd: 0.672, untracked_queries: 0, tracking_since: '2026-11-01T00:00:00Z',
      deployed: true, price_per_1k_queries: '7.00', ...ws,
    },
  };
}

function section(o: Partial<DashboardWebSearch> = {}): DashboardWebSearch {
  return {
    period: '2026-11', deployed: true, price_per_1k_queries: '7.00', queries: 96, cost_usd: 0.672,
    untracked_queries: 0, successful_requests: 480, elapsed_days: 20, avg_queries_per_day: 4.8,
    queries_per_request: 0.2,
    by_client: [
      { client: 'claude-code', queries: 60, cost_usd: 0.42 },
      { client: 'cowork', queries: 30, cost_usd: 0.21 },
      { client: 'codex', queries: 6, cost_usd: 0.042 },
    ],
    daily: [{ date: '2026-11-03', queries: 40 }, { date: '2026-11-20', queries: 56 }],
    ...o,
  };
}

function provider(node: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz="Asia/Kolkata">{node}</ReportingTimezoneProvider>
    </NextIntlClientProvider>,
  );
}

async function cards(k: DashboardKPI | null) {
  const el = await CostBudgetCards({ kpi: k, period: '2026-11', client: 'all', now: NOW });
  provider(el);
  const card = (title: string) => screen.getByText(title).closest('a, div.glass') as HTMLElement;
  return {
    usage: card('Usage This Month'),
    budget: card('Budget Utilization'),
    avg: card('Avg Cost Per User'),
    daily: card('Daily Average'),
  };
}

describe('COST & BUDGET 줄 — 표시 규칙 4가지', () => {
  it('A) 배포됨·사용 있음 — 모델 + 웹 검색 합산, 부제 분해, 예산 사용률은 모델만', async () => {
    const c = await cards(kpi({}));
    // 12.00 + 0.672 = 12.672
    expect(within(c.usage).getByText('$12.67')).toBeTruthy();
    expect(within(c.usage).getByText('Model $12.00 · Web search $0.67 (all users)')).toBeTruthy();
    expect(within(c.avg).getByText('$3.17')).toBeTruthy(); // 12.672 / 4
    expect(within(c.avg).getByText('Based on 4 active users this month · incl. web search')).toBeTruthy();
    // 12.672 / 20 = 0.6336, 월말 0.6336 × 30 = 19.008
    expect(within(c.daily).getByText('$0.63')).toBeTruthy();
    expect(within(c.daily).getByText('Month-end projection $19.01 (linear estimate) · incl. web search')).toBeTruthy();
    expect(within(c.budget).getByText('50.0%')).toBeTruthy();
    expect(within(c.budget).getByText('Model cost vs budget · web search excluded')).toBeTruthy();
  });

  it('B) 배포됨·검색 0 — 웹 검색 $0.00 로 합산 표기', async () => {
    const c = await cards(kpi({ queries: 0, cost_usd: 0 }));
    expect(within(c.usage).getByText('$12.00')).toBeTruthy();
    expect(within(c.usage).getByText('Model $12.00 · Web search $0.00 (all users)')).toBeTruthy();
    expect(within(c.budget).getByText('Model cost vs budget · web search excluded')).toBeTruthy();
  });

  it('배포됨·비용 미추적(NULL) — $0.00 이 아니라 "Web search not tracked"', async () => {
    const c = await cards(kpi({ queries: 3, cost_usd: null, untracked_queries: 3 }));
    expect(within(c.usage).getByText('$12.00')).toBeTruthy();
    expect(within(c.usage).getByText('Model $12.00 · Web search not tracked (all users)')).toBeTruthy();
    expect(within(c.avg).getByText('Based on 4 active users this month · web search not tracked')).toBeTruthy();
    expect(within(c.daily).getByText('Month-end projection $18.00 (linear estimate) · web search not tracked')).toBeTruthy();
    expect(within(c.budget).getByText('Model cost vs budget · web search excluded')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/Web search \$0\.00|incl\. web search/);
  });

  it('C) 미배포·기록 비용 있음 — 합산 표기', async () => {
    const c = await cards(kpi({ deployed: false }));
    expect(within(c.usage).getByText('$12.67')).toBeTruthy();
    expect(within(c.avg).getByText(/incl\. web search/)).toBeTruthy();
  });

  it.each([
    ['미배포·비용 0', { deployed: false, cost_usd: 0 }],
    ['미배포·미추적', { deployed: false, cost_usd: null, queries: 5, untracked_queries: 5 }],
    ['구 백엔드(web_search 없음)', undefined],
  ])('D) %s — 기능 이전과 똑같은 4장', async (_, ws) => {
    const c = await cards(kpi(ws as Partial<WebSearchUsage> | undefined));
    expect(within(c.usage).getByText('$12.00')).toBeTruthy();
    expect(within(c.usage).getByText('Monthly cumulative cost in USD (all users)')).toBeTruthy();
    expect(within(c.avg).getByText('$3.00')).toBeTruthy(); // 백엔드 cost_per_user_usd 그대로
    expect(within(c.avg).getByText('Based on 4 active users this month')).toBeTruthy();
    expect(within(c.daily).getByText('$0.60')).toBeTruthy(); // 12 / 20
    expect(within(c.daily).getByText('Month-end projection $18.00 (linear estimate)')).toBeTruthy();
    expect(within(c.budget).getByText('Usage ratio against total budget')).toBeTruthy();
    expect(document.body.textContent).not.toMatch(/web search/i);
  });

  it('조회 실패 — 전부 —', async () => {
    await cards(null);
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(4);
  });
});

describe('예산 사용률은 웹 검색을 무시한다(회귀)', () => {
  it.each([0, 0.672, 999])('웹 검색 비용 $%s 에도 budget_utilization_pct 그대로', (cost) => {
    const v = costBudgetView(kpi({ cost_usd: cost }), '2026-11', NOW);
    expect(v.budgetUtilization).toBe(50);
    expect(v.modelCost).toBe(12);
  });

  it('합산은 표시 전용 — 입력 KPI 의 예산·모델 값을 바꾸지 않는다', () => {
    const k = kpi({});
    const before = JSON.stringify(k);
    costBudgetView(k, '2026-11', NOW);
    expect(JSON.stringify(k)).toBe(before);
  });

  it('미추적(null) 비용은 합산에 0 으로', () => {
    expect(costBudgetView(kpi({ cost_usd: null }), '2026-11', NOW).usageCost).toBe(12);
  });
});

describe('computeDailyAvg', () => {
  it('당월은 경과일·월말 예상, 지난 달은 일수·예상 없음', () => {
    expect(computeDailyAvg('2026-11', 30, NOW)).toEqual({ dailyAvg: 1.5, projection: 45 });
    expect(computeDailyAvg('2026-10', 31, NOW)).toEqual({ dailyAvg: 1, projection: null });
  });
});

describe('WEB SEARCH · AGENTCORE 섹션', () => {
  it('A) 세 칸 — 앱별 도넛·범례, 비용 KPI·부제·일평균·요청당, 일별 막대', () => {
    provider(<WebSearchSection data={section()} period="2026-11" today="2026-11-20" />);
    const s = screen.getByTestId('web-search-section');
    expect(within(s).getByText('Web search · AgentCore')).toBeTruthy();
    expect(screen.getByTestId('web-search-center').textContent).toBe('96');
    expect(within(s).getByText('this month')).toBeTruthy();
    // 가운데 글자는 구멍(지름 94px) 안 72px 상자 — 최대 두 줄로 줄바꿈, 링에 닿지 않는다
    const label = screen.getByTestId('web-search-center-label');
    expect((label.parentElement as HTMLElement).style.width).toBe('72px');
    expect(label.className).toContain('line-clamp-2');
    const legend = [...screen.getByTestId('web-search-legend').querySelectorAll('li')].map((li) => li.textContent);
    expect(legend).toEqual(['Claude Code6062.5%', 'Cowork3031.3%', 'Codex66.2%']); // 합 100.0% 로 맞춘 반올림
    expect(screen.getByTestId('web-search-cost').textContent).toBe('$0.67');
    expect(screen.getByTestId('web-search-cost-subtitle').textContent).toBe('96 queries × $7.00 per 1,000');
    expect(screen.getByTestId('web-search-avg-day').textContent).toBe('4.8');
    expect(screen.getByTestId('web-search-per-request').textContent).toBe('0.20');
    expect(within(s).getByText('Daily queries')).toBeTruthy();
    expect(within(s).getByRole('button', { name: 'About web search pricing' })).toBeTruthy();
    // 섹션에 보이는 무료분·예산 안내 문장·추적 시작 문구 없음(툴팁 안 문장만 존재).
    const visible = s.cloneNode(true) as HTMLElement;
    visible.querySelectorAll('[role="tooltip"]').forEach((n) => n.remove());
    expect(visible.textContent).not.toMatch(/5,000|budget|tracking|since/i);
    expect(screen.queryByTestId('web-search-not-deployed')).toBeNull();
  });

  it('B) 배포됨·검색 0 — 0 / $0.00 로 보인다', () => {
    provider(
      <WebSearchSection
        data={section({ queries: 0, cost_usd: 0, by_client: [], daily: [], avg_queries_per_day: 0,
          queries_per_request: 0 })}
        period="2026-11" today="2026-11-20"
      />,
    );
    expect(screen.getByTestId('web-search-center').textContent).toBe('0');
    expect(screen.getByTestId('web-search-cost').textContent).toBe('$0.00');
    expect(screen.getByTestId('web-search-cost-subtitle').textContent).toBe('0 queries × $7.00 per 1,000');
    expect(screen.getByTestId('web-search-per-request').textContent).toBe('0.00');
  });

  it('C) 미배포·기록 비용 있음 — 배지', () => {
    provider(<WebSearchSection data={section({ deployed: false })} period="2026-11" today="2026-11-20" />);
    expect(screen.getByTestId('web-search-not-deployed').textContent).toBe('Web search is not deployed now');
  });

  it.each([
    ['비용 0', { deployed: false, cost_usd: 0 }],
    ['미추적', { deployed: false, cost_usd: null }],
  ])('D) 미배포·%s — 섹션 없음', (_, o) => {
    const { container } = provider(
      <WebSearchSection data={section(o as Partial<DashboardWebSearch>)} period="2026-11" today="2026-11-20" />,
    );
    expect(container.textContent).toBe('');
  });

  it('요청당 — 0.01 미만은 유효숫자 2개(3 / 1,818 → 0.0017), 0.00 으로 뭉개지 않는다', () => {
    provider(
      <WebSearchSection
        data={section({ queries: 3, successful_requests: 1818, queries_per_request: 3 / 1818 })}
        period="2026-11" today="2026-11-20"
      />,
    );
    expect(screen.getByTestId('web-search-per-request').textContent).toBe('0.0017');
  });

  it('도넛은 고정 152px 상자(축소 안 됨) — 범례를 덮지 않는다', () => {
    provider(<WebSearchSection data={section()} period="2026-11" today="2026-11-20" />);
    const box = screen.getByTestId('web-search-donut');
    expect(box.style.width).toBe('152px');
    expect(box.style.height).toBe('152px');
    expect(box.className).toContain('shrink-0');
    // 범례는 도넛 상자 밖(형제)에 있다
    expect(box.contains(screen.getByTestId('web-search-legend'))).toBe(false);
  });

  it('요청 0건이면 요청당 —, 미래 달이면 일평균 —', () => {
    provider(
      <WebSearchSection
        data={section({ queries_per_request: null, avg_queries_per_day: null })}
        period="2026-11" today="2026-11-20"
      />,
    );
    expect(screen.getByTestId('web-search-per-request').textContent).toBe('—');
    expect(screen.getByTestId('web-search-avg-day').textContent).toBe('—');
  });

  it('일별 막대 데이터 — 기간 전체 축, 지난 빈 날 0, 오늘 이후 null', () => {
    provider(<WebSearchSection data={section()} period="2026-11" today="2026-11-20" />);
    const rows = JSON.parse(screen.getByTestId('bar-data').textContent ?? '[]') as { date: string; queries: number | null }[];
    expect(rows).toHaveLength(30);
    expect(rows[0]).toMatchObject({ date: '2026-11-01', queries: 0 });
    expect(rows[2]).toMatchObject({ date: '2026-11-03', queries: 40 });
    expect(rows[19]).toMatchObject({ date: '2026-11-20', queries: 56 });
    expect(rows[20]).toMatchObject({ date: '2026-11-21', queries: null });
  });
});

describe('buildDailyQuerySeries', () => {
  it('지난 달은 전부 그린다(오늘 이후가 없음), 라벨 MM-DD', () => {
    const rows = buildDailyQuerySeries('2026-02', [{ date: '2026-02-14', queries: 3 }], '2026-11-20');
    expect(rows).toHaveLength(28);
    expect(rows.every((r) => r.queries !== null)).toBe(true);
    expect(rows[13]).toEqual({ date: '2026-02-14', label: '02-14', queries: 3 });
  });

  it('미래 달은 전부 null', () => {
    expect(buildDailyQuerySeries('2026-12', [], '2026-11-20').every((r) => r.queries === null)).toBe(true);
  });
});
