// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** /users 정책 요약의 출처(팀 지정 / 개별 지정 / 팀 상속 / 조직 기본값 / 없음)와 모델 묶음. */

import { describe, expect, it } from 'vitest';

import {
  appsSummary,
  blockedModels,
  budgetSummary,
  downgradeSummary,
  modelsSummary,
  rateSummary,
  unavailableChainModels,
  webSearchSummary,
} from '@/components/users/detail/policyRows';
import { changedCount, duplicateNames, familyOf, groupModels } from '@/components/users/detail/modelGroups';
import type { EffectivePolicy, ModelListItem, WebSearchQuotaEffective } from '@/types/entities';

const policy = (o: Partial<EffectivePolicy> = {}): EffectivePolicy => ({
  user_id: 'u1',
  email: 'u@x.io',
  team_id: 't1',
  team_name: 'Dev',
  allowed_clients: null,
  allowed_clients_source: 'none',
  allowed_models: null,
  allowed_models_source: 'none',
  web_search: {},
  cells: [],
  budgets: [],
  rate_limits: [],
  downgrade_rules: [],
  ...o,
});

const model = (alias: string, display_name: string | null, model_id = alias) =>
  ({ alias, display_name, model_id }) as unknown as ModelListItem;

describe('앱 접근 출처', () => {
  it.each([
    ['user', 'user', 'own'],
    ['team', 'user', 'team'],
    ['team', 'team', 'own'],
    ['organization', 'user', 'org'],
    ['organization', 'team', 'org'],
    ['none', 'team', 'none'],
  ] as const)('source=%s · scope=%s → %s', (src, scope, want) => {
    const s = appsSummary(policy({ allowed_clients_source: src, allowed_clients: src === 'none' ? null : ['codex'] }), scope);
    expect(s.source).toBe(want);
    expect(s.allowed).toEqual(src === 'none' ? null : ['codex']);
  });
});

describe('허용 모델', () => {
  const cat = [model('a', 'A'), model('b', 'B'), model('c', 'C')];
  it('목록이 없으면 모두 허용 · 출처 없음', () => {
    expect(modelsSummary(policy(), cat, 'team')).toEqual({ allowedCount: 3, total: 3, blocked: [], source: 'none' });
  });
  it('사용자 뷰: 팀 목록은 팀 상속, 자기 목록은 개별 지정', () => {
    const inh = modelsSummary(policy({ allowed_models: ['a'], allowed_models_source: 'team' }), cat, 'user');
    expect(inh).toMatchObject({ allowedCount: 1, blocked: ['b', 'c'], source: 'team' });
    expect(modelsSummary(policy({ allowed_models: ['a', 'b'], allowed_models_source: 'user' }), cat, 'user').source).toBe('own');
    expect(modelsSummary(policy({ allowed_models: ['a'], allowed_models_source: 'team' }), cat, 'team').source).toBe('own');
  });
});

describe('Rate limit — 사용자 → 팀 → 조직(GLOBAL)', () => {
  const rl = (scope: string, rpm: number | null, cph: string | null = null) => ({
    scope,
    model_alias: null,
    rpm_limit: rpm,
    tpm_limit: null,
    cpm_limit_usd: null,
    cph_limit_usd: cph,
  });
  it('사용자 설정이 있으면 개별 지정, 항목별로 가까운 레벨 값', () => {
    const s = rateSummary(policy({ rate_limits: [rl('USER', 10), rl('TEAM', 60, '5.00'), rl('GLOBAL', 100)] }), 'user');
    expect(s.source).toBe('own');
    expect(s.effective).toEqual({ rpm: 10, tpm: null, cpm: null, cph: 5 });
    expect(s.team).toEqual({ rpm: 60, tpm: null, cpm: null, cph: 5 });
  });
  it('사용자 없음 → 팀 상속, 팀도 없음 → 조직, 아무것도 없음 → 없음', () => {
    expect(rateSummary(policy({ rate_limits: [rl('TEAM', 60)] }), 'user').source).toBe('team');
    expect(rateSummary(policy({ rate_limits: [rl('GLOBAL', 100)] }), 'user').source).toBe('org');
    expect(rateSummary(policy({ rate_limits: [rl('GLOBAL', 100)] }), 'team').source).toBe('org');
    expect(rateSummary(policy(), 'team').source).toBe('none');
  });
  it('모델별 한도는 요약에 넣지 않는다', () => {
    const s = rateSummary(policy({ rate_limits: [{ ...rl('USER', 5), model_alias: 'opus' }] }), 'user');
    expect(s.source).toBe('none');
  });
});

describe('웹 검색 한도 · 예산 · 다운그레이드 출처', () => {
  const q = (o: Partial<WebSearchQuotaEffective> = {}): WebSearchQuotaEffective => ({
    period: '2026-11', monthly_limit: null, monthly_limit_source: 'none', used: 3, remaining: null,
    team_pool_limit: null, team_used: 3, team_remaining: null, per_request_cap: 5, per_request_cap_source: 'default', ...o,
  });
  it('웹 검색', () => {
    expect(webSearchSummary(q({ monthly_limit: 20, monthly_limit_source: 'user' }), 'user')?.source).toBe('own');
    expect(webSearchSummary(q({ per_request_cap: 3, per_request_cap_source: 'team' }), 'user')?.source).toBe('team');
    expect(webSearchSummary(q({ monthly_limit: 50, monthly_limit_source: 'organization' }), 'user')?.source).toBe('org');
    expect(webSearchSummary(q({ team_pool_limit: 100 }), 'team')).toMatchObject({ source: 'own', monthlyLimit: 100, used: 3 });
    expect(webSearchSummary(q(), 'team')?.source).toBe('none');
    expect(webSearchSummary(null, 'team')).toBeNull();
  });
  it('예산 — 사용자는 개인 cap 이 없으면 팀 예산을 함께 쓴다', () => {
    const b = (scope: string, max: string, used: string) => ({ scope, client: null, max_budget_usd: max, used_usd: used, policy: 'HARD_BLOCK' });
    expect(budgetSummary(policy({ budgets: [b('TEAM', '10', '0.16')] }), 'user')).toMatchObject({ source: 'team', max: null, teamMax: 10, teamUsed: 0.16 });
    expect(budgetSummary(policy({ budgets: [b('USER', '5', '1'), b('TEAM', '10', '2')] }), 'user').source).toBe('own');
    expect(budgetSummary(policy({ budgets: [b('TEAM', '10', '2')] }), 'team')).toMatchObject({ source: 'own', max: 10, used: 2 });
    expect(budgetSummary(policy(), 'team').source).toBe('none');
  });
  it('다운그레이드 — 임계 순서, 팀 규칙은 팀 뷰에선 팀 지정·사용자 뷰에선 상속', () => {
    const p = policy({
      downgrade_rules: [
        { scope: 'TEAM', threshold_pct: 95, from_model_alias: 's', to_model_alias: 'h' },
        { scope: 'TEAM', threshold_pct: 80, from_model_alias: 'o', to_model_alias: 's' },
      ],
    });
    expect(downgradeSummary(p, 'team').steps.map((s) => s.threshold)).toEqual([80, 95]);
    expect(downgradeSummary(p, 'team').source).toBe('own');
    expect(downgradeSummary(p, 'user').source).toBe('team');
  });
});

describe('유효 정책 — 차단 모델·쓸 수 없는 다운그레이드 단계', () => {
  const p = policy({
    cells: [
      { client: 'claude-code', model_alias: 'opus', allowed: false, blocked_by: ['user_model'] },
      { client: 'codex', model_alias: 'opus', allowed: false, blocked_by: ['user_model', 'model_app'] },
      { client: 'claude-code', model_alias: 'haiku', allowed: true, blocked_by: [] },
      { client: 'codex', model_alias: 'gpt', allowed: false, blocked_by: ['model_app'] },
      { client: 'claude-code', model_alias: 'gpt', allowed: true, blocked_by: [] },
    ],
    downgrade_rules: [{ scope: 'TEAM', threshold_pct: 80, from_model_alias: 'opus', to_model_alias: 'haiku' }],
  });
  it('어느 앱에서도 못 쓰는 모델만 차단 — 막은 정책 축을 모은다', () => {
    const r = blockedModels(p);
    expect(r.blocked).toEqual([{ alias: 'opus', reasons: ['model_app', 'user_model'] }]);
    expect(r.allowed).toEqual(['gpt', 'haiku']);
  });
  it('체인의 시작 모델이 막혀 있으면 경고 대상', () => {
    expect(unavailableChainModels(p)).toEqual(['opus']);
  });
});

describe('모델 묶음', () => {
  const cat = [
    model('claude-opus-4-8', 'Claude Code · Opus 4.8', 'global.anthropic.claude-opus-4-8'),
    model('global.anthropic.claude-opus-4-8', 'Claude Code · Opus 4.8'),
    model('claude-haiku-5-5', 'Claude Haiku 5.5'),
    model('codex-gpt-5.5', 'Codex · GPT-5.5'),
    model('llama-3-70b', 'Llama 3 70B'),
  ];
  it('계열', () => {
    expect(cat.map((m) => familyOf(m))).toEqual(['opus', 'opus', 'haiku', 'gpt', 'open']);
  });
  it('같은 표시명은 이름 중복', () => {
    expect([...duplicateNames(cat)]).toEqual(['Claude Code · Opus 4.8']);
  });
  it('검색·필터 — 그룹 순서 Haiku → Sonnet → Opus → GPT → 오픈', () => {
    const checked = new Set(['claude-haiku-5-5', 'codex-gpt-5.5']);
    expect(groupModels(cat, checked, '', 'all').map((g) => g.family)).toEqual(['haiku', 'opus', 'gpt', 'open']);
    expect(groupModels(cat, checked, '', 'blocked').flatMap((g) => g.items.map((m) => m.alias))).toEqual([
      'claude-opus-4-8',
      'global.anthropic.claude-opus-4-8',
      'llama-3-70b',
    ]);
    expect(groupModels(cat, checked, 'GLOBAL.', 'all').flatMap((g) => g.items.map((m) => m.alias))).toEqual([
      'global.anthropic.claude-opus-4-8',
    ]);
  });
  it('변경 수 = 추가 + 제거', () => {
    expect(changedCount(new Set(['a', 'b']), new Set(['b', 'c', 'd']))).toBe(3);
  });
});
