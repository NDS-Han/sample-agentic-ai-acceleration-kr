// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * EffectivePolicyCard — 다운그레이드 체인의 모델이 매트릭스에서 전부 ✗ 이면
 * 회색·취소선 노드 + "사용 불가" 경고를 보여야 한다 (P1-3).
 * 다운그레이드 규칙과 모델 허용 정책은 따로 저장돼 서로 검증되지 않는다.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { EffectivePolicyCard } from '@/components/users/EffectivePolicyCard';
import type { EffectivePolicy } from '@/types/entities';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string, vars?: Record<string, unknown>) =>
    vars ? `${key}:${JSON.stringify(vars)}` : key,
}));
vi.mock('@/lib/actions/users', () => ({
  getEffectivePolicyAction: vi.fn(),
  getTeamEffectivePolicyAction: vi.fn(),
}));

function policy(overrides: Partial<EffectivePolicy> = {}): EffectivePolicy {
  const cell = (client: string, alias: string, allowed: boolean) => ({
    client,
    model_alias: alias,
    allowed,
    blocked_by: allowed ? [] : ['user_model'],
  });
  return {
    user_id: null,
    email: null,
    team_id: 't1',
    team_name: 'Developers',
    allowed_clients: null,
    allowed_clients_source: 'none',
    allowed_models: ['sonnet', 'haiku'],
    allowed_models_source: 'team',
    web_search: {},
    cells: [
      cell('claude-code', 'opus', false),
      cell('codex', 'opus', false),
      cell('claude-code', 'sonnet', true),
      cell('codex', 'sonnet', false),
      cell('claude-code', 'haiku', true),
      cell('codex', 'haiku', true),
    ],
    budgets: [],
    rate_limits: [],
    downgrade_rules: [
      { scope: 'TEAM', threshold_pct: 80, from_model_alias: 'opus', to_model_alias: 'sonnet' },
      { scope: 'TEAM', threshold_pct: 90, from_model_alias: 'sonnet', to_model_alias: 'haiku' },
    ],
    models: [],
    ...overrides,
  } as EffectivePolicy;
}

describe('EffectivePolicyCard — 체인 ↔ 매트릭스 교차 표시', () => {
  it('모든 앱에서 ✗ 인 체인 모델만 취소선 + 팀 경고', () => {
    render(<EffectivePolicyCard policy={policy()} />);
    // 노드(다이어그램) — opus 만 취소선. 매트릭스 셀에도 같은 텍스트가 있으니 font-mono 노드로 한정.
    const nodes = screen
      .getAllByText(/^(opus|sonnet|haiku)$/)
      .filter((el) => el.className.includes('font-mono'));
    const struck = nodes.filter((el) => el.className.includes('line-through')).map((el) => el.textContent);
    expect(struck).toEqual(['opus']);
    expect(screen.getByText(/chainUnavailableTeam/)).toBeInTheDocument();
    expect(screen.getByRole('note').textContent).toContain('"models":"opus"');
  });

  it('사용자 뷰는 사용자 문구를 쓴다', () => {
    render(<EffectivePolicyCard policy={policy({ user_id: 'u1' })} />);
    expect(screen.getByText(/chainUnavailableUser/)).toBeInTheDocument();
  });

  it('매트릭스에 없는(비활성) 체인 모델도 사용 불가로 본다', () => {
    const p = policy({
      downgrade_rules: [
        { scope: 'TEAM', threshold_pct: 80, from_model_alias: 'gone', to_model_alias: 'haiku' },
      ],
    });
    render(<EffectivePolicyCard policy={p} />);
    expect(screen.getByRole('note').textContent).toContain('"models":"gone"');
  });

  it('체인 모델이 모두 쓸 수 있으면 경고가 없다', () => {
    const p = policy({
      downgrade_rules: [
        { scope: 'TEAM', threshold_pct: 90, from_model_alias: 'sonnet', to_model_alias: 'haiku' },
      ],
    });
    render(<EffectivePolicyCard policy={p} />);
    expect(screen.queryByRole('note')).not.toBeInTheDocument();
  });
});

describe('EffectivePolicyCard — 모델 라벨 (P2-8)', () => {
  const models = [
    { alias: 'opus', output_price_per_1k: 0.075, display_name: 'Opus 5.5' },
    { alias: 'sonnet', output_price_per_1k: 0.015, display_name: 'Sonnet 5' },
    { alias: 'haiku', output_price_per_1k: 0.005, display_name: null },
  ];

  it('매트릭스 행은 display_name 우선 + alias 보조 줄, 표시명이 없으면 alias 한 줄', () => {
    render(<EffectivePolicyCard policy={policy()} models={models} />);
    const row = screen.getByRole('row', { name: /Sonnet 5/ });
    expect(row).toHaveTextContent('Sonnet 5');
    expect(row).toHaveTextContent('sonnet');
    const haikuRow = screen.getAllByRole('row').find((r) => r.textContent?.startsWith('haiku'));
    expect(haikuRow).toBeDefined();
  });

  it('다운그레이드 노드·경고 문구도 표시명을 쓰고 alias 는 title 로 남긴다', () => {
    render(<EffectivePolicyCard policy={policy()} models={models} />);
    const node = screen
      .getAllByText('Opus 5.5')
      .find((el) => el.className.includes('line-through'))!;
    expect(node).toBeDefined();
    expect(node.parentElement!.getAttribute('title')).toBe('opus — chainUnavailableTeam');
    expect(screen.getByRole('note').textContent).toContain('"models":"Opus 5.5"');
  });

  it('models 가 없으면(비-ADMIN /my) 응답 내장 display_name 또는 alias 로 폴백한다', () => {
    render(<EffectivePolicyCard policy={policy()} />);
    expect(screen.getByRole('note').textContent).toContain('"models":"opus"');
  });
});
