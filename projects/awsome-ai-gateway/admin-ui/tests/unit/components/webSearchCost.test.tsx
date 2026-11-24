// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AgentCore 웹 검색 비용(모니터링 전용) — 화면 표시 규칙.
 *
 *  - 표시 규칙: 배포(deployed)됐거나 기록 비용 > 0 일 때만 보인다. 아니면 기능 이전 그대로.
 *  - null 비용 = "Not tracked" — "$0.00" 으로 그리지 않는다. 0 = 추적 중·검색 없음.
 *  - 무료분·정가 안내는 ⓘ 툴팁만, "tracking since" 류 문구는 어디에도 없다.
 *  - 앱 점유: "N web searches · $X", 모니터링·Body Logs: 모델 비용과 웹 검색 분리.
 *  - 단가 카드: 입력 검증, 문자열로 저장 요청, 미배포 배지.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import ko from '../../../messages/ko.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import {
  WebSearchCostValue,
  WebSearchInline,
  WebSearchPriceInfo,
} from '@/components/common/WebSearchCost';
import { ClientShareDonutClient } from '@/components/dashboard/ClientShareDonutClient';
import { MonitoringOverview } from '@/components/monitoring/MonitoringOverview';
import { BodyRecordContent } from '@/components/monitoring/bodyLogRender';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';
import { WebSearchPricingCard } from '@/components/models/WebSearchPricingCard';
import type { BodyLogResponse } from '@/lib/actions/body-logs';
import type { ClientShareResponse } from '@/lib/actions/dashboard';
import type { MonitoringOverviewResponse } from '@/lib/actions/monitoring';
import type { MyBudgetResponse } from '@/lib/actions/my';
import type { WebSearchPricingSetting } from '@/lib/actions/settings';
import { isValidWebSearchPrice } from '@/lib/utils/webSearchPrice';
import { webSearchNotDeployed, webSearchVisible } from '@/lib/utils/webSearchVisibility';
import type { WebSearchUsage } from '@/types/entities';

const setWebSearchPricingAction = vi.fn();
vi.mock('@/lib/actions/settings', () => ({
  setWebSearchPricingAction: (...a: unknown[]) => setWebSearchPricingAction(...a),
}));
const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast }) }));
// recharts 는 jsdom 에서 크기 0 이라 의미 없다 — 목록(li)만 검증한다.
vi.mock('recharts', () => ({ PieChart: () => null, Pie: () => null, Cell: () => null }));

beforeEach(() => {
  setWebSearchPricingAction.mockReset();
  toast.mockReset();
});

function wrap(node: ReactNode, locale: 'en' | 'ko' = 'en') {
  return render(
    <NextIntlClientProvider locale={locale} messages={(locale === 'en' ? en : ko) as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz="Asia/Kolkata">{node}</ReportingTimezoneProvider>
    </NextIntlClientProvider>,
  );
}

const WS = (o: Partial<WebSearchUsage> = {}): WebSearchUsage => ({
  queries: 0, cost_usd: 0, untracked_queries: 0, tracking_since: '2026-11-24T03:00:00Z',
  deployed: true, price_per_1k_queries: '7.00', ...o,
});

/** 화면 어디에도 "추적 시작" 류 문구가 없어야 한다. */
function expectNoTrackingText(text: string | null) {
  expect(text ?? '').not.toMatch(/tracking|추적 시작|since/i);
}

describe('표시 규칙', () => {
  it.each([
    ['배포됨·비용 있음', { deployed: true, cost_usd: 1 }, true, false],
    ['배포됨·검색 0', { deployed: true, cost_usd: 0 }, true, false],
    ['배포됨·미추적', { deployed: true, cost_usd: null }, true, false],
    ['미배포·기록 비용 있음', { deployed: false, cost_usd: 0.5 }, true, true],
    ['미배포·비용 0', { deployed: false, cost_usd: 0 }, false, false],
    ['미배포·미추적', { deployed: false, cost_usd: null }, false, false],
    ['구 백엔드(필드 없음)', undefined, false, false],
  ])('%s', (_, ws, visible, badge) => {
    expect(webSearchVisible(ws as never)).toBe(visible);
    expect(webSearchNotDeployed(ws as never)).toBe(badge);
  });
});

describe('WebSearchCostValue — NULL 과 0', () => {
  it('null 은 Not tracked — $ 금액으로 그리지 않는다', () => {
    const { container } = wrap(<WebSearchCostValue cost={null} />);
    expect(container.textContent).toBe('Not tracked');
    const title = container.querySelector('[data-web-search-cost="untracked"]')?.getAttribute('title');
    expect(title).toBe('No cost was recorded for these searches. That does not mean they were free.');
    expectNoTrackingText(title ?? '');
  });

  it('0 은 $0.00, 공용 fmtUsd 규칙(1센트 미만 < $0.01 + 툴팁, KPI 는 유효숫자)', () => {
    expect(wrap(<WebSearchCostValue cost={0} />).container.textContent).toBe('$0.00');
    const sub = wrap(<WebSearchCostValue cost={0.007} />).container;
    expect(sub.textContent).toBe('< $0.01');
    expect(sub.querySelector('[title]')?.getAttribute('title')).toBe('$0.0070');
    expect(wrap(<WebSearchCostValue cost={0.007} significant />).container.textContent).toBe('$0.0070');
  });

  it('ko 미추적 문구', () => {
    expect(wrap(<WebSearchCostValue cost={null} />, 'ko').container.textContent).toBe('미추적');
  });
});

describe('WebSearchInline', () => {
  it('검색 수 · 비용, 일부 미기록이면 그 수를 함께', () => {
    const { container } = wrap(<WebSearchInline queries={10} cost={0.028} untracked={6} />);
    expect(container.textContent).toBe('10 web searches · $0.03 · +6 queries without recorded cost');
  });

  it('검색 0회면 아무것도 그리지 않는다', () => {
    expect(wrap(<WebSearchInline queries={0} cost={0} />).container.textContent).toBe('');
  });
});

describe('ⓘ 무료분·정가 툴팁', () => {
  it('en — 지정 문구, 현재 단가', () => {
    wrap(<WebSearchPriceInfo price="7.00" />);
    expect(screen.getByRole('button', { name: 'About web search pricing' })).toBeTruthy();
    expect(screen.getByTestId('web-search-price-info').textContent).toBe(
      'AgentCore includes 5,000 free web search queries per month, shared across your AWS organization and regions. Costs here use the list price ($7.00 per 1,000), so your actual AWS bill can be lower.',
    );
  });

  it('ko', () => {
    wrap(<WebSearchPriceInfo price="6.5" />, 'ko');
    const text = screen.getByTestId('web-search-price-info').textContent ?? '';
    expect(text).toContain('월 5,000건');
    expect(text).toContain('AWS 조직의 모든 계정·리전');
    expect(text).toContain('1,000건당 $6.50');
  });
});

describe('대시보드 앱 점유 칩 — "N web searches · $X"', () => {
  const clients: ClientShareResponse['clients'] = [
    { client: 'claude-code', cost_usd: 1.0, share_pct: 66.67, call_count: 10, web_search_count: 96,
      web_search_cost_usd: 0.672, web_search_untracked_count: 0 },
    { client: 'cowork', cost_usd: 0.5, share_pct: 33.33, call_count: 4, web_search_count: 2,
      web_search_cost_usd: null, web_search_untracked_count: 2 },
  ];

  it('보일 때 — 같은 줄에 비용, 미추적은 Not tracked, 모델 비용 열 불변', () => {
    wrap(<ClientShareDonutClient data={{ period: '2026-11', total_cost_usd: 1.5, clients,
      web_search: WS({ queries: 98, cost_usd: 0.672 }) }} />);
    const cells = screen.getAllByTestId('client-web-search');
    expect(cells.map((c) => c.textContent)).toEqual(['96 web searches · $0.67', '2 web searches · Not tracked']);
    expect(screen.getByText('$1.00')).toBeTruthy();
  });

  it('미배포·기록 비용 있음 → 보인다', () => {
    wrap(<ClientShareDonutClient data={{ period: '2026-11', total_cost_usd: 1.5, clients,
      web_search: WS({ deployed: false, queries: 98, cost_usd: 0.672 }) }} />);
    expect(screen.getAllByTestId('client-web-search')).toHaveLength(2);
  });

  it('미배포·기록 비용 없음 → 칩 전부 숨김(검색 수 문구도)', () => {
    const { container } = wrap(<ClientShareDonutClient data={{ period: '2026-11', total_cost_usd: 1.5,
      clients, web_search: WS({ deployed: false, queries: 98, cost_usd: null }) }} />);
    expect(screen.queryAllByTestId('client-web-search')).toHaveLength(0);
    expect(container.textContent).not.toContain('web search');
  });
});

describe('모니터링 1시간 요약', () => {
  const mon = (ws?: WebSearchUsage): MonitoringOverviewResponse => ({
    timestamp: '2026-11-24T04:00:00Z',
    active_models: 3,
    last_1h: { total_requests: 5, error_count: 0, error_rate_pct: 0, avg_latency_ms: 10,
      p95_latency_ms: 20, total_cost_usd: 0.5, web_search: ws },
  });

  it('보일 때 — 모델 비용 카드와 별개의 웹 검색 카드, 안내 문장 없음', () => {
    const { container } = wrap(<MonitoringOverview data={mon(WS({ queries: 4, cost_usd: 0.028 }))} />);
    expect(screen.getByText('Model cost')).toBeTruthy();
    expect(screen.getByText('Web search')).toBeTruthy();
    expect(screen.getByText('$0.03')).toBeTruthy();
    expect(screen.getByText('4 queries')).toBeTruthy();
    expect(container.textContent).not.toContain('5,000');
    expectNoTrackingText(container.textContent);
  });

  it('미배포·비용 있음 → 배지', () => {
    wrap(<MonitoringOverview data={mon(WS({ deployed: false, queries: 4, cost_usd: 0.028 }))} />);
    expect(screen.getByText('Web search is not deployed now')).toBeTruthy();
  });

  it('미배포·비용 없음 → 종전 그대로(웹 검색 카드·Model cost 부제 없음)', () => {
    wrap(<MonitoringOverview data={mon(WS({ deployed: false, queries: 4, cost_usd: null }))} />);
    expect(screen.queryByText('Web search')).toBeNull();
    expect(screen.queryByText('Model cost')).toBeNull();
  });
});

describe('내 예산 카드 — 모델 비용만 안내는 표시 규칙이 참일 때만', () => {
  const data: MyBudgetResponse = {
    user_id: 'u', period: '2026-11', team: null,
    budget: { limit_usd: 10, used_usd: 1, remaining_usd: 9, usage_pct: 10, policy: 'HARD_BLOCK' },
  };
  it('보일 때 안내', () => {
    wrap(<MyBudgetCard data={data} showWebSearchHint />);
    expect(screen.getByTestId('budget-model-cost-only').textContent).toBe(
      'Model cost only — web search is not counted toward budgets.',
    );
  });
  it('숨김일 때 종전 그대로', () => {
    wrap(<MyBudgetCard data={data} />);
    expect(screen.queryByTestId('budget-model-cost-only')).toBeNull();
  });
});

describe('Body Logs 요청 상세', () => {
  const base: BodyLogResponse = {
    found: true, request_id: 'r', source: 'firehose', key: 'k', duplicates: 0, oversized: false,
    model_alias: 'm', requested_at: '2026-11-24T00:00:00Z', cost_usd: 0.05, record: null,
  };

  it('배포됨 — "Model cost" + 별도 "Web search: N queries · $"', () => {
    wrap(<BodyRecordContent data={{ ...base, web_search_count: 3, web_search_cost_usd: 0.021,
      web_search_deployed: true }} />);
    expect(screen.getByText('Model cost')).toBeTruthy();
    expect(screen.getByTestId('body-log-web-search').textContent).toBe('Web search 3 queries · $0.02');
  });

  it('배포됨·비용 미기록 행 → Not tracked', () => {
    wrap(<BodyRecordContent data={{ ...base, web_search_count: 2, web_search_cost_usd: null,
      web_search_deployed: true }} />);
    expect(screen.getByTestId('body-log-web-search').textContent).toContain('Not tracked');
  });

  it('미배포·이 요청에 기록 비용 있음 → 보인다', () => {
    wrap(<BodyRecordContent data={{ ...base, web_search_count: 2, web_search_cost_usd: 0.014,
      web_search_deployed: false }} />);
    expect(screen.getByTestId('body-log-web-search')).toBeTruthy();
  });

  it('미배포·비용 없음 → 종전 라벨 그대로', () => {
    wrap(<BodyRecordContent data={{ ...base, web_search_count: 2, web_search_cost_usd: null,
      web_search_deployed: false }} />);
    expect(screen.queryByTestId('body-log-web-search')).toBeNull();
    expect(screen.queryByText('Model cost')).toBeNull();
  });
});

describe('웹 검색 단가 카드', () => {
  const setting: WebSearchPricingSetting = {
    price_per_1k_queries: '7.00', default_price_per_1k_queries: '7.00', is_default: true,
    updated_at: null, tracking_since: '2026-11-24T03:00:00Z', deployed: true,
  };

  it('입력 검증 — 범위·소수 4자리', () => {
    for (const ok of ['0', '7', '7.00', '5.1234', '1000']) expect(isValidWebSearchPrice(ok)).toBe(true);
    for (const bad of ['', '-1', '1000.01', '1.23456', 'abc', '1e3']) expect(isValidWebSearchPrice(bad)).toBe(false);
  });

  it('잘못된 값이면 저장 비활성 + 안내, 같은 값도 비활성, 추적 시작 문구 없음', () => {
    const { container } = wrap(<WebSearchPricingCard initial={setting} />);
    const input = screen.getByLabelText('Price per 1,000 queries (USD)') as HTMLInputElement;
    const save = screen.getByRole('button', { name: 'Save price' }) as HTMLButtonElement;
    expect(input.value).toBe('7');
    expect(save.disabled).toBe(true);
    fireEvent.change(input, { target: { value: '-2' } });
    expect(save.disabled).toBe(true);
    expect(screen.getByRole('alert').textContent).toContain('between 0 and 1,000');
    expect(screen.getByText('Using the AWS list price')).toBeTruthy();
    expect(screen.queryByText('Web search is not deployed now')).toBeNull();
    expect(container.textContent).not.toMatch(/tracking since/i);
  });

  it('미배포면 배지', () => {
    wrap(<WebSearchPricingCard initial={{ ...setting, deployed: false }} />);
    expect(screen.getByText('Web search is not deployed now')).toBeTruthy();
  });

  it('저장은 문자열로 요청하고 응답으로 갱신', async () => {
    setWebSearchPricingAction.mockResolvedValue({
      success: true,
      data: { ...setting, price_per_1k_queries: '6.5000', is_default: false, updated_at: '2026-11-24T05:00:00Z' },
    });
    wrap(<WebSearchPricingCard initial={setting} />);
    fireEvent.change(screen.getByLabelText('Price per 1,000 queries (USD)'), { target: { value: '6.5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save price' }));
    await waitFor(() => expect(setWebSearchPricingAction).toHaveBeenCalledWith('6.5'));
    await waitFor(() => expect(toast).toHaveBeenCalledWith(expect.objectContaining({ type: 'success' })));
    expect(screen.getByText(/AWS list price: \$7\.00 per 1,000 queries/)).toBeTruthy();
  });

  it('조회 실패면 실패 상태', () => {
    wrap(<WebSearchPricingCard initial={null} />);
    expect(screen.getByText('Could not load the web search price.')).toBeTruthy();
  });
});
