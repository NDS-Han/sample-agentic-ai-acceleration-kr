// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AgentCore 웹 검색 비용(모니터링 전용) — 화면 표시 규칙.
 *
 *  - null 비용 = "Not tracked"(추적 이전) — "$0.00" 으로 그리지 않는다
 *  - 0 = 추적 중·검색 없음 → $0.00, 1센트 미만은 공용 fmtUsd 규칙('< $0.01' + 툴팁)
 *  - 기간 일부만 추적됐으면 미추적 검색 수를 함께 적는다
 *  - 정가 계산 + 조직 공유 월 5,000건 무료 안내(en/ko), 예산 제외 안내
 *  - 앱 점유: "N web searches" 아래 비용, 오른쪽 비용·점유율은 모델 비용 그대로
 *  - 모니터링·Body Logs: 모델 비용과 웹 검색을 다른 라벨로 분리
 *  - 단가 카드: 입력 검증, 문자열로 저장 요청
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import ko from '../../../messages/ko.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import {
  WebSearchCostNote,
  WebSearchCostValue,
  WebSearchInline,
} from '@/components/common/WebSearchCost';
import { ClientShareDonutClient } from '@/components/dashboard/ClientShareDonutClient';
import { MonitoringOverview } from '@/components/monitoring/MonitoringOverview';
import { BodyRecordContent } from '@/components/monitoring/bodyLogRender';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';
import { WebSearchPricingCard } from '@/components/models/WebSearchPricingCard';
import type { BodyLogResponse } from '@/lib/actions/body-logs';
import type { ClientShareResponse } from '@/lib/actions/dashboard';
import type { MyBudgetResponse } from '@/lib/actions/my';
import type { WebSearchPricingSetting } from '@/lib/actions/settings';
import { isValidWebSearchPrice } from '@/lib/utils/webSearchPrice';

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

const SINCE = '2026-11-24T03:00:00+00:00';

describe('WebSearchCostValue — NULL 과 0', () => {
  it('null 은 Not tracked — $ 금액으로 그리지 않는다', () => {
    const { container } = wrap(<WebSearchCostValue cost={null} />);
    expect(container.textContent).toBe('Not tracked');
    expect(container.textContent).not.toContain('$');
    expect(container.querySelector('[data-web-search-cost="untracked"]')?.getAttribute('title')).toContain(
      'does not mean they were free',
    );
  });

  it('0 은 추적 중·검색 없음 → $0.00', () => {
    const { container } = wrap(<WebSearchCostValue cost={0} />);
    expect(container.textContent).toBe('$0.00');
  });

  it('공용 fmtUsd 규칙 — 1센트 미만은 < $0.01 + 정확값 툴팁, KPI 는 유효숫자', () => {
    const { container } = wrap(<WebSearchCostValue cost={0.007} />);
    expect(container.textContent).toBe('< $0.01');
    expect(container.querySelector('[title]')?.getAttribute('title')).toBe('$0.0070');
    const kpi = wrap(<WebSearchCostValue cost={0.007} significant />);
    expect(kpi.container.textContent).toBe('$0.0070');
    expect(wrap(<WebSearchCostValue cost={12.5} />).container.textContent).toBe('$12.50');
  });

  it('ko 미추적 문구', () => {
    expect(wrap(<WebSearchCostValue cost={null} />, 'ko').container.textContent).toBe('미추적');
  });
});

describe('WebSearchInline', () => {
  it('검색 수 · 비용, 일부 미추적이면 그 수를 함께', () => {
    const { container } = wrap(<WebSearchInline queries={10} cost={0.028} untracked={6} />);
    expect(container.textContent).toBe(
      '10 web searches · $0.03 · +6 queries before tracking started (cost not tracked)',
    );
  });

  it('검색 0회면 아무것도 그리지 않는다', () => {
    expect(wrap(<WebSearchInline queries={0} cost={0} />).container.textContent).toBe('');
  });
});

describe('WebSearchCostNote — 무료분·예산 제외·추적 시작', () => {
  it('en: 정가 계산, 조직 공유 월 5,000건 무료(실제 청구 더 낮을 수 있음), 예산 제외, 추적 시작 시각', () => {
    const { container } = wrap(<WebSearchCostNote ws={{ tracking_since: SINCE }} inline />);
    const text = container.textContent ?? '';
    expect(text).toContain('list price');
    expect(text).toContain('5,000 free web search queries per month');
    expect(text).toContain('shared by all accounts in your AWS organization');
    expect(text).toContain('may be lower');
    expect(text).toContain('not included in budgets or rate limits');
    // 리포팅 TZ(Asia/Kolkata) 기준 시각.
    expect(text).toContain('Cost tracked since 2026-11-24 08:30');
  });

  it('ko 안내 + 추적 미시작', () => {
    const { container } = wrap(<WebSearchCostNote ws={{ tracking_since: null }} inline />, 'ko');
    const text = container.textContent ?? '';
    expect(text).toContain('월 5,000건');
    expect(text).toContain('AWS 조직의 모든 계정');
    expect(text).toContain('예산과 요청 한도에 포함되지 않습니다');
    expect(text).toContain('아직 비용 추적이 시작되지 않았습니다');
  });

  it('툴팁형은 같은 안내를 role=tooltip 안에', () => {
    wrap(<WebSearchCostNote ws={{ tracking_since: SINCE }} />);
    expect(screen.getByRole('tooltip').textContent).toContain('5,000 free web search queries');
    expect(screen.getByRole('button', { name: 'About web search cost' })).toBeTruthy();
  });
});

describe('대시보드 앱 점유 — 검색 수 아래 비용, 모델 비용·점유율 불변', () => {
  const data: ClientShareResponse = {
    period: '2026-11',
    total_cost_usd: 1.5,
    clients: [
      { client: 'claude-code', cost_usd: 1.0, share_pct: 66.67, call_count: 10, web_search_count: 3,
        web_search_cost_usd: 0.021, web_search_untracked_count: 0 },
      { client: 'cowork', cost_usd: 0.5, share_pct: 33.33, call_count: 4, web_search_count: 2,
        web_search_cost_usd: null, web_search_untracked_count: 2 },
      { client: 'codex', cost_usd: 0, share_pct: 0, call_count: 1, web_search_count: 0,
        web_search_cost_usd: 0, web_search_untracked_count: 0 },
    ],
    web_search: { queries: 5, cost_usd: 0.021, untracked_queries: 2, tracking_since: SINCE },
  };

  it('기존 "N web searches" 문구 + 그 비용, 미추적 앱은 Not tracked', () => {
    wrap(<ClientShareDonutClient data={data} />);
    const cells = screen.getAllByTestId('client-web-search');
    expect(cells).toHaveLength(2);
    expect(cells[0].textContent).toBe('3 web searches$0.02');
    expect(cells[1].textContent).toBe('2 web searchesNot tracked');
    // 모델 비용 열은 그대로(웹 검색 미포함).
    expect(screen.getByText('$1.00')).toBeTruthy();
    expect(screen.getByText(/Cost and share are model cost only/)).toBeTruthy();
  });

  it('구 백엔드(web_search_cost_usd 없음) — 검색 수만, 비용 칸 없음', () => {
    const legacy: ClientShareResponse = {
      period: '2026-11',
      total_cost_usd: 1,
      clients: [{ client: 'claude-code', cost_usd: 1, share_pct: 100, call_count: 1, web_search_count: 3 }],
    };
    wrap(<ClientShareDonutClient data={legacy} />);
    expect(screen.getByTestId('client-web-search').textContent).toBe('3 web searches');
  });
});

describe('모니터링 1시간 요약', () => {
  it('모델 비용 카드와 별개의 웹 검색 카드', () => {
    wrap(
      <MonitoringOverview
        data={{
          timestamp: '2026-11-24T04:00:00Z',
          active_models: 3,
          last_1h: {
            total_requests: 5, error_count: 0, error_rate_pct: 0, avg_latency_ms: 10,
            p95_latency_ms: 20, total_cost_usd: 0.5,
            web_search: { queries: 4, cost_usd: 0.028, untracked_queries: 0, tracking_since: SINCE },
          },
        }}
      />,
    );
    expect(screen.getByText('Model cost')).toBeTruthy();
    expect(screen.getByText('Web search')).toBeTruthy();
    expect(screen.getByText('$0.03')).toBeTruthy();
    expect(screen.getByText('4 queries')).toBeTruthy();
    expect(screen.getByTestId('web-search-cost-note').textContent).toContain('5,000 free');
  });
});

describe('예산 화면은 모델 비용만 + 안내', () => {
  it('내 예산 카드에 "웹 검색은 예산 미포함" 안내', () => {
    const data: MyBudgetResponse = {
      user_id: 'u', period: '2026-11', team: null,
      budget: { limit_usd: 10, used_usd: 1, remaining_usd: 9, usage_pct: 10, policy: 'HARD_BLOCK' },
    };
    wrap(<MyBudgetCard data={data} />);
    expect(screen.getByTestId('budget-model-cost-only').textContent).toBe(
      'Model cost only — web search is not counted toward budgets.',
    );
  });
});

describe('Body Logs 요청 상세', () => {
  const base: BodyLogResponse = {
    found: true, request_id: 'r', source: 'firehose', key: 'k', duplicates: 0, oversized: false,
    model_alias: 'm', requested_at: '2026-11-24T00:00:00Z', cost_usd: 0.05, record: null,
  };

  it('검색이 있으면 "Model cost" + 별도 "Web search" (추적됨)', () => {
    wrap(<BodyRecordContent data={{ ...base, web_search_count: 3, web_search_cost_usd: 0.021 }} />);
    expect(screen.getByText('Model cost')).toBeTruthy();
    const ws = screen.getByTestId('body-log-web-search');
    expect(ws.textContent).toContain('Web search');
    expect(ws.textContent).toContain('3 queries');
    expect(ws.textContent).toContain('$0.02');
  });

  it('추적 이전 행은 Not tracked', () => {
    wrap(<BodyRecordContent data={{ ...base, web_search_count: 2, web_search_cost_usd: null }} />);
    expect(screen.getByTestId('body-log-web-search').textContent).toContain('Not tracked');
  });

  it('검색 없으면 종전 라벨 그대로', () => {
    wrap(<BodyRecordContent data={{ ...base, web_search_count: 0, web_search_cost_usd: 0 }} />);
    expect(screen.queryByTestId('body-log-web-search')).toBeNull();
    expect(screen.queryByText('Model cost')).toBeNull();
  });
});

describe('웹 검색 단가 카드', () => {
  const setting: WebSearchPricingSetting = {
    price_per_1k_queries: '7.00', default_price_per_1k_queries: '7.00', is_default: true,
    updated_at: null, tracking_since: null,
  };

  it('입력 검증 — 범위·소수 4자리', () => {
    for (const ok of ['0', '7', '7.00', '5.1234', '1000']) expect(isValidWebSearchPrice(ok)).toBe(true);
    for (const bad of ['', '-1', '1000.01', '1.23456', 'abc', '1e3']) expect(isValidWebSearchPrice(bad)).toBe(false);
  });

  it('잘못된 값이면 저장 비활성 + 안내, 같은 값도 비활성', () => {
    wrap(<WebSearchPricingCard initial={setting} />);
    const input = screen.getByLabelText('Price per 1,000 queries (USD)') as HTMLInputElement;
    const save = screen.getByRole('button', { name: 'Save price' }) as HTMLButtonElement;
    expect(input.value).toBe('7');
    expect(save.disabled).toBe(true); // 변경 없음
    fireEvent.change(input, { target: { value: '-2' } });
    expect(save.disabled).toBe(true);
    expect(screen.getByRole('alert').textContent).toContain('between 0 and 1,000');
    expect(screen.getByText('Using the AWS list price')).toBeTruthy();
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
