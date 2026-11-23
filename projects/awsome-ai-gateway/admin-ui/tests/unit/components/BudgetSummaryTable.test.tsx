// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * BudgetSummaryTable 사용자 행 — 팀 예산만 적용되는 사용자는 '-' 대신 팀 한도/잔여,
 * 개별 cap 이 팀 한도보다 크면 "팀 한도가 먼저 적용됨" 배지 (P1-4).
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { BudgetSummaryTable } from '@/components/budgets/BudgetSummaryTable';
import type { BudgetSummaryItem } from '@/types/entities';
import { AlertLevel, BudgetScope } from '@/types/enums';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));
vi.mock('@/components/budgets/AutoDowngradeConfig', () => ({ AutoDowngradeConfig: () => null }));
vi.mock('@/components/budgets/TeamAllocationView', () => ({ TeamAllocationView: () => null }));
vi.mock('@/components/budgets/SetBudgetDialog', () => ({ SetBudgetDialog: () => null }));

// jsdom 에는 scrollIntoView 가 없다 — focusTeam 딥링크 effect 용 스텁.
Element.prototype.scrollIntoView = vi.fn();

const base = {
  is_active: true,
  used: 1,
  usage_pct: null,
  alert_level: AlertLevel.NORMAL,
} as const;

const items: BudgetSummaryItem[] = [
  {
    ...base,
    target_id: 't1',
    target_type: BudgetScope.TEAM,
    target_name: 'NDS_Developers',
    limit: 15,
    remaining: 12,
    usage_pct: 20,
  },
  {
    ...base,
    target_id: 'u-big',
    target_type: BudgetScope.USER,
    target_name: 'big-cap',
    team_id: 't1',
    limit: 50,
    remaining: 49,
    cap_source: 'individual',
  },
  {
    ...base,
    target_id: 'u-team',
    target_type: BudgetScope.USER,
    target_name: 'team-only',
    team_id: 't1',
    limit: null,
    remaining: null,
    cap_source: null,
  },
] as BudgetSummaryItem[];

describe('BudgetSummaryTable — 팀 한도 표시', () => {
  it('팀 예산만 적용되는 사용자는 팀 한도·팀 잔여, cap>팀 한도면 경고 배지', () => {
    render(<BudgetSummaryTable items={items} isAdmin={false} models={[]} focusTeam="t1" />);
    const teamOnlyRow = screen.getByText('team-only').closest('tr')!;
    expect(within(teamOnlyRow).getByText('$15.00')).toBeInTheDocument();
    expect(within(teamOnlyRow).getByText('teamBudgetApplied')).toBeInTheDocument();
    expect(within(teamOnlyRow).getByText('$12.00')).toBeInTheDocument();
    expect(within(teamOnlyRow).getByText('teamRemainingCaption')).toBeInTheDocument();
    expect(within(teamOnlyRow).queryByText('teamLimitFirst')).not.toBeInTheDocument();

    const bigRow = screen.getByText('big-cap').closest('tr')!;
    expect(within(bigRow).getByText('teamLimitFirst')).toBeInTheDocument();
  });
});
