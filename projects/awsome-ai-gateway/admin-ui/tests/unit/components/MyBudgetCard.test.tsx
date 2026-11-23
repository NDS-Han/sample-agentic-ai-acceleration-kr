// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * MyBudgetCard 한도 문구 — 금액은 fmtUsd 결과를 그대로 넣는다.
 * 회귀: fmtUsd(cap).slice(1) 로 '$' 를 떼고 메시지에서 다시 붙였는데, 1센트 미만
 * 한도의 '< $0.01' 은 slice(1) 이 ' $0.01' 이 돼 'Limit: $ $0.01' 로 깨졌다.
 */

import { render, screen } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { describe, expect, it } from 'vitest';

import messages from '../../../messages/en.json';
import { MyBudgetCard } from '@/components/my/MyBudgetCard';
import type { MyBudgetResponse } from '@/lib/actions/my';

function renderCard(limit: number | null, teamDefault: number | null = null) {
  const data: MyBudgetResponse = {
    user_id: 'u1',
    period: '2026-11',
    team:
      teamDefault == null
        ? null
        : {
            team_id: 't1',
            team_name: 'T',
            limit_usd: 100,
            used_usd: 0,
            remaining_usd: 100,
            usage_pct: 0,
            default_cap_usd: teamDefault,
          },
    budget: {
      limit_usd: limit,
      used_usd: 0,
      remaining_usd: limit,
      usage_pct: limit ? 0 : null,
      policy: limit != null ? 'HARD_BLOCK' : null,
    },
  };
  return render(
    <NextIntlClientProvider locale="en" messages={messages as unknown as AbstractIntlMessages}>
      <MyBudgetCard data={data} />
    </NextIntlClientProvider>,
  );
}

describe('MyBudgetCard limit text', () => {
  it('일반 금액 — $ 한 번', () => {
    renderCard(1234.5);
    expect(screen.getByText('Limit: $1,234.50')).toBeTruthy();
  });

  it('1센트 미만 한도 — "$ $0.01" 이 아니라 "< $0.01", 툴팁에 정확한 값', () => {
    renderCard(0.004);
    const el = screen.getByText('Limit: < $0.01');
    expect(el.getAttribute('title')).toBe('$0.0040');
    expect(screen.queryByText(/\$ \$/)).toBeNull();
  });

  it('팀 기본 cap 문구도 같은 규칙', () => {
    renderCard(null, 0.004);
    expect(screen.getByText('Limit: < $0.01 (team default)')).toBeTruthy();
  });
});
