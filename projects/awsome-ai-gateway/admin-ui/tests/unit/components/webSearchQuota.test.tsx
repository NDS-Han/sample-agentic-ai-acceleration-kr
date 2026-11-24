// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 웹 검색 한도(검색 수 기준, 예산과 별개) — Budget Management 섹션·유효 정책 표시.
 *
 *  - 조직 기본·팀 공용 풀(진행 막대)·사용자 개별 지정, 빈 칸 = 상속, 0 = 사용 안 함
 *  - 저장은 숫자/null 로 요청, 잘못된 입력은 저장 불가 + 안내, 해제는 행 제거
 *  - Users/Teams 패널·Effective Policy: 유효 월 한도·사용·잔여·팀 풀·요청당 상한
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { WebSearchQuotaSection, parseQuotaInput, quotaUsagePct } from '@/components/budgets/WebSearchQuotaSection';
import { EffectivePolicyCard } from '@/components/users/EffectivePolicyCard';
import type { EffectivePolicy, WebSearchQuotaEffective, WebSearchQuotaOverview } from '@/types/entities';

const setAction = vi.fn();
const clearAction = vi.fn();
const searchUsers = vi.fn();
vi.mock('@/lib/actions/webSearchQuota', () => ({
  setWebSearchQuotaAction: (...a: unknown[]) => setAction(...a),
  clearWebSearchQuotaAction: (...a: unknown[]) => clearAction(...a),
}));
vi.mock('@/lib/actions/users', () => ({
  searchUsersAction: (...a: unknown[]) => searchUsers(...a),
  getEffectivePolicyAction: vi.fn(),
  getTeamEffectivePolicyAction: vi.fn(),
}));
const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast }) }));

beforeEach(() => {
  setAction.mockReset().mockResolvedValue({ success: true, data: {} });
  clearAction.mockReset().mockResolvedValue({ success: true, data: { cleared: true } });
  searchUsers.mockReset();
  toast.mockReset();
});

function wrap(node: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>{node}</NextIntlClientProvider>,
  );
}

const OVERVIEW: WebSearchQuotaOverview = {
  period: '2026-11',
  default_per_request_cap: 5,
  org: { monthly_query_limit: 200, per_request_cap: null, updated_at: null },
  org_used: 321,
  teams: [
    { team_id: '00000000-0000-0000-0000-0000000000b1', team_name: 'Platform', dept_name: 'Eng',
      monthly_query_limit: 1000, per_request_cap: 3, used: 800 },
    { team_id: '00000000-0000-0000-0000-0000000000b2', team_name: 'Sales', dept_name: null,
      monthly_query_limit: null, per_request_cap: null, used: 12 },
  ],
  users: [
    { user_id: '00000000-0000-0000-0000-0000000000a1', email: 'kim@example.com', display_name: 'Kim',
      team_name: 'Platform', monthly_query_limit: 0, per_request_cap: null, used: 4 },
  ],
};

describe('parseQuotaInput', () => {
  it('빈 칸 = 상속(null), 범위 안 정수만', () => {
    expect(parseQuotaInput('', 0, 10)).toBeNull();
    expect(parseQuotaInput(' 7 ', 0, 10)).toBe(7);
    expect(parseQuotaInput('0', 0, 10)).toBe(0);
    for (const bad of ['-1', '1.5', 'x', '11']) expect(parseQuotaInput(bad, 0, 10)).toBeUndefined();
    expect(parseQuotaInput('0', 1, 50)).toBeUndefined();
  });
});

describe('quotaUsagePct', () => {
  it('한도 없음·사용 안 함(0)은 null(—), 그 외 %', () => {
    expect(quotaUsagePct(5, null)).toBeNull();
    expect(quotaUsagePct(5, 0)).toBeNull();
    expect(quotaUsagePct(800, 1000)).toBe(80);
  });
});

describe('Budget Management — 웹 검색 한도 섹션(예산 표와 같은 레이아웃)', () => {
  const TEAM_NAMES = { '00000000-0000-0000-0000-0000000000b1': 'Eng_Platform' };
  const teamRows = () => within(screen.getByTestId('ws-quota-teams')).getAllByRole('row').slice(1);
  const userRows = () => within(screen.getByTestId('ws-quota-users')).getAllByRole('row').slice(1);
  const cells = (row: HTMLElement) => within(row).getAllByRole('cell').map((c) => c.textContent);

  it('조직 요약 — 이번 달 검색 큰 숫자 · 사용자별 월 기본 한도 · 기본 요청당 상한 · 한도 설정', () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    expect(screen.getByTestId('ws-quota-org-used').textContent).toBe('321');
    expect(screen.getByTestId('ws-quota-org-limit').textContent).toBe('200');
    expect(screen.getByTestId('ws-quota-org-cap').textContent).toBe('Default 5'); // 상속 → 게이트웨이 기본
    expect(within(screen.getByTestId('ws-quota-org')).getByRole('button', { name: 'Set limit' })).toBeTruthy();
    expect(screen.getByText(/limited by the number of searches, not by cost/)).toBeTruthy();
  });

  it('팀 표 — 열 머리와 셀이 정렬된 열, 이름은 예산 표와 같은 표시 이름', () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} teamNames={TEAM_NAMES} />);
    const heads = within(screen.getByTestId('ws-quota-teams')).getAllByRole('columnheader').map((h) => h.textContent);
    expect(heads[0]).toBe('Target');
    expect(heads[1]).toBe('Searches this month');
    expect(heads[2]).toContain('Monthly limit (team pool)');
    expect(heads[3]).toBe('Usage');
    expect(heads[4]).toContain('Per-request cap');
    expect(heads[5]).toBe('Actions');
    // 긴 설명은 열 머리 툴팁에만 — 행에는 반복하지 않는다
    expect(screen.getAllByRole('tooltip').some((t) => t.textContent?.includes("Counts all members' searches"))).toBe(true);
    const [platform, sales] = teamRows();
    expect(cells(platform)).toEqual(['Eng_Platform', '800', '1,000', expect.stringContaining('80.0%'), '3', 'Set limit']);
    expect(cells(sales)).toEqual(['Sales', '12', 'Unlimited', '—', 'Default 5', 'Set limit']);
    expect(sales.textContent).not.toMatch(/team pool|Team pool/);
    // 숫자 열은 우측 정렬
    expect(within(platform).getAllByRole('cell')[1].className).toMatch(/text-right/);
  });

  it('사용자 표 — 이름 + email · team, 0 은 Off, 사용률 —', () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    const [kim] = userRows();
    expect(cells(kim)).toEqual(['Kimkim@example.com · Platform', '4', 'Off', '—', 'Default 5', 'Set limit']);
  });

  it('팀 한도 설정 대화상자 — 단위·상속 placeholder, 저장/취소 나란히, 숫자로 저장', async () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set limit for Sales' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Web search limit · Sales')).toBeTruthy();
    const monthly = within(dialog).getByLabelText('Monthly limit (team pool)') as HTMLInputElement;
    const cap = within(dialog).getByLabelText('Per-request cap') as HTMLInputElement;
    expect(monthly.placeholder).toBe('Inherited: Unlimited');
    expect(cap.placeholder).toBe('Inherited: 5');
    expect(within(dialog).getByText('searches/month')).toBeTruthy();
    expect(within(dialog).getByText('searches/request')).toBeTruthy();
    // 저장·취소는 같은 줄 컨테이너
    const save = within(dialog).getByRole('button', { name: 'Save' });
    const cancel = within(dialog).getByRole('button', { name: 'Cancel' });
    expect(save.parentElement).toBe(cancel.parentElement);
    // 지정이 없는 팀은 해제 버튼 없음
    expect(within(dialog).queryByRole('button', { name: 'Remove override' })).toBeNull();
    fireEvent.change(monthly, { target: { value: '300' } });
    fireEvent.click(save);
    await waitFor(() => expect(setAction).toHaveBeenCalledWith(
      'teams', '00000000-0000-0000-0000-0000000000b2', { monthly_query_limit: 300, per_request_cap: null }));
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(cells(teamRows().find((r) => r.textContent?.startsWith('Sales'))!).slice(0, 4))
      .toEqual(['Sales', '12', '300', expect.stringContaining('4.0%')]);
  });

  it('검증 — 음수·소수·문자 월 한도와 범위 밖 상한은 저장 불가 + 안내, 비우기(상속)', async () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    fireEvent.click(within(screen.getByTestId('ws-quota-org')).getByRole('button', { name: 'Set limit' }));
    const dialog = await screen.findByRole('dialog');
    const monthly = within(dialog).getByLabelText('Monthly limit per user') as HTMLInputElement;
    const cap = within(dialog).getByLabelText('Per-request cap') as HTMLInputElement;
    expect(monthly.value).toBe('200');
    const save = within(dialog).getByRole('button', { name: 'Save' }) as HTMLButtonElement;
    for (const bad of ['-1', '1.5', 'abc']) {
      fireEvent.change(monthly, { target: { value: bad } });
      expect(save.disabled).toBe(true);
      expect(within(dialog).getByText('Enter a whole number, 0 or more.')).toBeTruthy();
    }
    fireEvent.change(monthly, { target: { value: '0' } });  // 0 = 사용 안 함 → 허용
    expect(save.disabled).toBe(false);
    fireEvent.change(cap, { target: { value: '51' } });
    expect(save.disabled).toBe(true);
    expect(within(dialog).getByText('Enter a whole number from 1 to 50.')).toBeTruthy();
    // 비우기(상속) — 입력을 비운다
    fireEvent.click(within(dialog).getAllByRole('button', { name: 'Clear (inherit)' })[1]);
    expect(cap.value).toBe('');
    expect(save.disabled).toBe(false);
  });

  it('Esc 는 저장 없이 닫는다', async () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set limit for Kim' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.keyDown(dialog, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
    expect(setAction).not.toHaveBeenCalled();
  });

  it('사용자 지정 해제 → 표에서 제거', async () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    fireEvent.click(screen.getByRole('button', { name: 'Set limit for Kim' }));
    const dialog = await screen.findByRole('dialog');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Remove override' }));
    await waitFor(() => expect(clearAction).toHaveBeenCalledWith('users', '00000000-0000-0000-0000-0000000000a1'));
    await waitFor(() => expect(screen.getByText('No user overrides.')).toBeTruthy());
  });

  it('사용자 검색 → 대화상자에서 저장하면 표에 추가', async () => {
    searchUsers.mockResolvedValue({ success: true, data: { items: [
      { id: '00000000-0000-0000-0000-0000000000a9', email: 'lee@example.com', display_name: 'Lee',
        role: 'DEVELOPER', team_id: null, team_name: 'Sales' }], truncated: false } });
    wrap(<WebSearchQuotaSection initial={{ ...OVERVIEW, users: [] }} />);
    fireEvent.change(screen.getByLabelText('Search users by name or email'), { target: { value: 'lee' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Lee · lee@example.com' }));
    const dialog = await screen.findByRole('dialog');
    expect((within(dialog).getByLabelText('Monthly limit') as HTMLInputElement).placeholder).toBe('Inherited: 200');
    fireEvent.change(within(dialog).getByLabelText('Monthly limit'), { target: { value: '25' } });
    fireEvent.change(within(dialog).getByLabelText('Per-request cap'), { target: { value: '2' } });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(setAction).toHaveBeenCalledWith(
      'users', '00000000-0000-0000-0000-0000000000a9', { monthly_query_limit: 25, per_request_cap: 2 }));
    await waitFor(() => expect(userRows()[0].textContent).toContain('lee@example.com · Sales'));
    expect(cells(userRows()[0]).slice(1, 3)).toEqual(['0', '25']);
  });

  it('조회 실패 상태', () => {
    wrap(<WebSearchQuotaSection initial={null} />);
    expect(screen.getByText('Could not load web search quotas.')).toBeTruthy();
  });

  it('ko — 단위·상속·사용 안 함 문구', async () => {
    const ko = (await import('../../../messages/ko.json')).default;
    render(
      <NextIntlClientProvider locale="ko" messages={ko as unknown as AbstractIntlMessages}>
        <WebSearchQuotaSection initial={OVERVIEW} />
      </NextIntlClientProvider>,
    );
    expect(screen.getByText(/금액이 아니라 검색 횟수로 제한합니다/)).toBeTruthy();
    expect(cells(userRows()[0])[2]).toBe('사용 안 함');
    fireEvent.click(screen.getByRole('button', { name: 'Sales 한도 설정' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('회/월')).toBeTruthy();
    expect(within(dialog).getByText('회/요청')).toBeTruthy();
    expect((within(dialog).getByLabelText('월 한도 (팀 공용)') as HTMLInputElement).placeholder).toBe('상속: 무제한');
    expect((within(dialog).getByLabelText('요청당 상한') as HTMLInputElement).placeholder).toBe('상속: 5');
  });
});

describe('Effective Policy — 웹 검색 한도', () => {
  const q = (o: Partial<WebSearchQuotaEffective> = {}): WebSearchQuotaEffective => ({
    period: '2026-11', monthly_limit: 50, monthly_limit_source: 'organization', used: 20, remaining: 30,
    team_pool_limit: 1000, team_used: 800, team_remaining: 200, per_request_cap: 3,
    per_request_cap_source: 'team', ...o,
  });
  const policy = (o: Partial<EffectivePolicy>): EffectivePolicy => ({
    user_id: 'u1', email: 'u@x', team_id: 't1', team_name: 'Platform', allowed_clients: null,
    allowed_clients_source: 'none', allowed_models: null, allowed_models_source: 'none',
    web_search: {}, cells: [], budgets: [], rate_limits: [], downgrade_rules: [], models: [], ...o,
  }) as EffectivePolicy;

  it('사용자 뷰 — 월 한도·사용·잔여(출처), 팀 풀, 요청당 상한(출처)', () => {
    wrap(<EffectivePolicyCard policy={policy({ web_search_quota: q() })} />);
    expect(screen.getByTestId('ws-quota-monthly').textContent).toBe(
      'Monthly limit (organization default)20 / 50 used · 30 left');
    expect(screen.getByTestId('ws-quota-team').textContent).toBe('Team pool800 / 1,000 used · 200 left');
    expect(screen.getByTestId('ws-quota-cap').textContent).toBe('Per-request cap (team)3 searches');
  });

  it('무제한·팀 풀 없음', () => {
    wrap(<EffectivePolicyCard policy={policy({ web_search_quota: q({ monthly_limit: null,
      monthly_limit_source: 'none', remaining: null, team_pool_limit: null, team_remaining: null }) })} />);
    expect(screen.getByTestId('ws-quota-monthly').textContent).toContain('Unlimited · 20 used');
    expect(screen.getByTestId('ws-quota-team').textContent).toContain('No team pool · 800 used');
  });

  it('팀 뷰 — 개인 한도 줄 없이 팀 풀과 상한', () => {
    wrap(<EffectivePolicyCard policy={policy({ user_id: null, web_search_quota: q() })} />);
    expect(screen.queryByTestId('ws-quota-monthly')).toBeNull();
    expect(screen.getByTestId('ws-quota-team')).toBeTruthy();
  });

  it('구 백엔드(필드 없음)면 그리지 않는다', () => {
    wrap(<EffectivePolicyCard policy={policy({})} />);
    expect(screen.queryByTestId('effective-web-search-quota')).toBeNull();
  });
});
