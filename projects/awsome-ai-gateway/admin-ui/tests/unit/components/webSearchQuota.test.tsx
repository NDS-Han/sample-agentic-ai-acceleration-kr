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
import { WebSearchQuotaSection, parseQuotaInput } from '@/components/budgets/WebSearchQuotaSection';
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

describe('Budget Management — 웹 검색 한도 섹션', () => {
  it('조직 기본·팀 공용 풀·사용자 개별 지정과 이번 달 사용', () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    const org = screen.getByTestId('ws-quota-org');
    expect(org.textContent).toContain('321 searches this month across the organization');
    expect(org.textContent).toContain('Monthly limit per user: 200');
    expect(org.textContent).toContain('Per-request cap: Default 5');      // 상속 → 게이트웨이 기본
    const teams = within(screen.getByTestId('ws-quota-teams')).getAllByRole('listitem');
    expect(teams[0].textContent).toContain('Platform');
    expect(within(teams[0]).getByTestId('ws-quota-used').textContent).toContain('800 / 1,000');
    expect(teams[1].textContent).toContain('Team pool (monthly, shared): Unlimited');
    const users = within(screen.getByTestId('ws-quota-users')).getAllByRole('listitem');
    expect(users[0].textContent).toContain('kim@example.com');
    expect(users[0].textContent).toContain('Monthly limit: Off');      // 0 = 사용 안 함
  });

  it('팀 풀 편집 → 숫자로 저장, 빈 칸은 null(상속)', async () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    const sales = within(screen.getByTestId('ws-quota-teams')).getAllByRole('listitem')[1];
    fireEvent.click(within(sales).getByRole('button', { name: 'Edit' }));
    fireEvent.change(within(sales).getByLabelText('Team pool (monthly, shared)'), { target: { value: '300' } });
    fireEvent.click(within(sales).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(setAction).toHaveBeenCalledWith(
      'teams', '00000000-0000-0000-0000-0000000000b2', { monthly_query_limit: 300, per_request_cap: null }));
    await waitFor(() => expect(sales.textContent).toContain('Team pool (monthly, shared): 300'));
  });

  it('잘못된 입력은 저장 불가 + 안내', () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    const org = screen.getByTestId('ws-quota-org');
    fireEvent.click(within(org).getByRole('button', { name: 'Edit' }));
    fireEvent.change(within(org).getByLabelText('Per-request cap'), { target: { value: '0' } });
    expect((within(org).getByRole('button', { name: 'Save' }) as HTMLButtonElement).disabled).toBe(true);
    expect(within(org).getByRole('alert').textContent).toContain('per-request cap 1–50');
  });

  it('사용자 개별 지정 해제 → 목록에서 제거', async () => {
    wrap(<WebSearchQuotaSection initial={OVERVIEW} />);
    const users = screen.getByTestId('ws-quota-users');
    fireEvent.click(within(users).getByRole('button', { name: 'Clear' }));
    await waitFor(() => expect(clearAction).toHaveBeenCalledWith('users', '00000000-0000-0000-0000-0000000000a1'));
    await waitFor(() => expect(within(users).getByText('No user overrides.')).toBeTruthy());
  });

  it('사용자 검색 → 개별 지정 추가', async () => {
    searchUsers.mockResolvedValue({ success: true, data: { items: [
      { id: '00000000-0000-0000-0000-0000000000a9', email: 'lee@example.com', display_name: 'Lee',
        role: 'DEVELOPER', team_id: null, team_name: 'Sales' }], truncated: false } });
    wrap(<WebSearchQuotaSection initial={{ ...OVERVIEW, users: [] }} />);
    fireEvent.change(screen.getByLabelText('Search users by name or email'), { target: { value: 'lee' } });
    fireEvent.click(await screen.findByRole('button', { name: 'Lee · lee@example.com' }));
    const users = screen.getByTestId('ws-quota-users');
    fireEvent.change(within(users).getByLabelText('Monthly limit'), { target: { value: '25' } });
    fireEvent.change(within(users).getByLabelText('Per-request cap'), { target: { value: '2' } });
    fireEvent.click(within(users).getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(setAction).toHaveBeenCalledWith(
      'users', '00000000-0000-0000-0000-0000000000a9', { monthly_query_limit: 25, per_request_cap: 2 }));
    await waitFor(() => expect(users.textContent).toContain('lee@example.com · Sales'));
  });

  it('조회 실패 상태', () => {
    wrap(<WebSearchQuotaSection initial={null} />);
    expect(screen.getByText('Could not load web search quotas.')).toBeTruthy();
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
