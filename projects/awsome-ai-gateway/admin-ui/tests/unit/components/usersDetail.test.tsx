// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 팀·사용자 상세.
 *  - 팀: 전체 팀 이름(예산 관리와 같은 표시), 사용량 카드, 정책 요약 줄의 출처 배지, 구성원 표(행 → 사용자),
 *        팀장 변경(합성 이메일 대신 이름 + 미인증 표기 — P3-25 유지)
 *  - 사용자: 이름(없으면 이메일)·이메일·역할·팀·마지막 사용, 바로가기, 출처 '팀 정책 상속'/'개별 지정',
 *        유효 정책 타일·차단 칩·접힌 매트릭스(앱 열 표시 이름)
 */

import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { ReportingTimezoneProvider } from '@/components/common/ReportingTimezoneProvider';
import { TeamDetail } from '@/components/users/detail/TeamDetail';
import { UserDetail } from '@/components/users/detail/UserDetail';
import type { EffectivePolicy, OrgTreeNode } from '@/types/entities';
import { fmtDateMinute } from '@/lib/utils/format';
import { tzZoneLabel, withTzAbbr } from '@/lib/utils/timezone';

// 시점(마지막 사용·마지막 동기화)은 브라우저 TZ + 이름 — 보고 TZ(Asia/Kolkata)가 아니다.
const BROWSER_TZ = Intl.DateTimeFormat().resolvedOptions().timeZone;
const stampZ = (iso: string) => withTzAbbr(fmtDateMinute(iso, BROWSER_TZ), BROWSER_TZ, iso);

const { users, models } = vi.hoisted(() => ({
  users: {
    getTeamEffectivePolicyAction: vi.fn(),
    getEffectivePolicyAction: vi.fn(),
    getUsersActivityAction: vi.fn(),
    forceReauthTeamAction: vi.fn(),
    setTeamLeaderAction: vi.fn(),
    unsetTeamLeaderAction: vi.fn(),
    getUserAllowedClientsAction: vi.fn(),
    setUserAllowedClientsAction: vi.fn(),
    getScopeAllowedClientsAction: vi.fn(),
    setScopeAllowedClientsAction: vi.fn(),
    getUserAllowedModelsAction: vi.fn(),
    setUserAllowedModelsAction: vi.fn(),
  },
  models: { listActiveModelsAction: vi.fn(), getTeamAllowedModelsAction: vi.fn(), setTeamAllowedModelsAction: vi.fn() },
}));
vi.mock('@/lib/actions/users', () => users);
vi.mock('@/lib/actions/models', () => models);
vi.mock('@/lib/actions/rate-limits', () => ({
  getRateLimitStatusAction: vi.fn(),
  setRateLimitAction: vi.fn(),
  deleteRateLimitAction: vi.fn(),
}));
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast: vi.fn() }) }));
vi.mock('next/navigation', () => ({ useRouter: () => ({ refresh: vi.fn(), push: vi.fn() }) }));

const ok = <T,>(data: T) => ({ success: true as const, data });
const meta = (o: Partial<OrgTreeNode['meta']> = {}): OrgTreeNode['meta'] => ({
  member_count: null, team_count: null, leader_name: null, leader_user_id: null, email: null, role: null, team_name: null, ...o,
});
const U1 = '11111111-1111-4111-8111-111111111111';
const U2 = '22222222-2222-4222-8222-222222222222';
const TEAM: OrgTreeNode = {
  id: 'dev',
  name: 'Developers',
  type: 'TEAM',
  meta: meta({ member_count: 2, team_display_name: 'NDS_Developers' }),
  children: [
    { id: U1, name: 'sub-123', type: 'USER', children: [], meta: meta({ email: 'sub-123@unknown', role: 'TEAM_LEADER', team_name: 'Developers' }) },
    {
      id: U2,
      name: 'seunghwan',
      type: 'USER',
      children: [],
      meta: meta({ email: 'seunghwan@nongshim.co.kr', role: 'DEVELOPER', team_name: 'Developers', team_display_name: 'NDS_Developers', has_custom_policies: true }),
    },
  ],
};

const policy = (o: Partial<EffectivePolicy> = {}): EffectivePolicy => ({
  user_id: null,
  email: null,
  team_id: 'dev',
  team_name: 'Developers',
  allowed_clients: null,
  allowed_clients_source: 'none',
  allowed_models: ['claude-haiku-5-5'],
  allowed_models_source: 'team',
  web_search: { 'claude-code': true },
  cells: [
    { client: 'claude-code', model_alias: 'claude-haiku-5-5', allowed: true, blocked_by: [] },
    { client: 'claude-code', model_alias: 'claude-opus-4-8', allowed: false, blocked_by: ['user_model'] },
  ],
  budgets: [{ scope: 'TEAM', client: null, max_budget_usd: '10.00', used_usd: '0.16', policy: 'HARD_BLOCK' }],
  rate_limits: [],
  downgrade_rules: [{ scope: 'TEAM', threshold_pct: 80, from_model_alias: 'claude-opus-4-8', to_model_alias: 'claude-haiku-5-5' }],
  web_search_quota: {
    period: '2026-11', monthly_limit: null, monthly_limit_source: 'none', used: 3, remaining: null, team_pool_limit: null,
    team_used: 3, team_remaining: null, per_request_cap: 5, per_request_cap_source: 'default',
  },
  ...o,
});

const cell = (cost: number, tokens: number, requests: number) => ({
  input_tokens: tokens, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0, total_tokens: tokens, cost_usd: cost, requests,
});

let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => {
  Object.values({ ...users, ...models }).forEach((f) => f.mockReset());
  models.listActiveModelsAction.mockResolvedValue(
    ok([
      { alias: 'claude-haiku-5-5', display_name: 'Claude Haiku 5.5', model_id: 'h' },
      { alias: 'claude-opus-4-8', display_name: 'Claude Code · Opus 4.8', model_id: 'o' },
    ]),
  );
  users.getUsersActivityAction.mockResolvedValue(
    ok({ period: '2026-11', items: [{ user_id: U2, last_used_at: '2026-11-06T08:50:00Z', web_searches: 3 }, { user_id: U1, last_used_at: null, web_searches: 0 }] }),
  );
  fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const u = new URL(String(input), 'http://x');
    const body = u.pathname.endsWith(`/${U2}`)
      ? { user_id: U2, total: { ...cell(0.16, 747_000, 17), avg_latency_ms: 0 }, models: [
          { model: 'claude-haiku-5-5', display_name: 'Claude Haiku 5.5', ...cell(0.03, 604_000, 13), avg_latency_ms: 0 },
          { model: 'claude-sonnet-5-5', display_name: 'Claude Sonnet 5.5', ...cell(0.13, 143_000, 4), avg_latency_ms: 0 },
        ] }
      : { rows: [{ user_id: U2, name: 'seunghwan', email: 'seunghwan@nongshim.co.kr', team_name: 'Developers', cells: {}, other: null, total: cell(0.16, 747_000, 17) }], total_users: 1, page: 1, page_size: 50 };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  });
  vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

function wrap(node: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
      <ReportingTimezoneProvider tz="Asia/Kolkata">{node}</ReportingTimezoneProvider>
    </NextIntlClientProvider>,
  );
}

describe('팀 상세', () => {
  it('전체 팀 이름·카드·정책 출처·구성원 표', async () => {
    users.getTeamEffectivePolicyAction.mockResolvedValue(ok(policy()));
    const onOpenUser = vi.fn();
    wrap(<TeamDetail node={TEAM} breadcrumb={['Default Organization', 'NDS']} onOpenUser={onOpenUser} onChanged={vi.fn()} />);
    expect(screen.getByTestId('team-title').textContent).toBe('NDS_Developers');
    expect(screen.getByText('Default Organization › NDS')).toBeTruthy();

    const kpis = screen.getByTestId('team-kpis');
    await within(kpis).findByText('$0.16');
    expect(within(kpis).getByText('1.6% of team budget $10.00')).toBeTruthy();
    expect(within(kpis).getByText('747K')).toBeTruthy();
    expect(within(kpis).getByText('1 / 2')).toBeTruthy();
    expect(within(kpis).getByText('Team pool: unlimited')).toBeTruthy();
    // 사용자-모델 API 는 팀 범위로
    expect(fetchMock.mock.calls.some((c) => String(c[0]).includes('scope=team%3Adev'))).toBe(true);

    const src = (id: string) => screen.getByTestId(id).querySelector('[data-source]')?.getAttribute('data-source');
    await screen.findByTestId('policy-models');
    expect(src('policy-apps')).toBe('none');
    expect(src('policy-models')).toBe('own');
    expect(within(screen.getByTestId('policy-models')).getByText('1 of 2 allowed')).toBeTruthy();
    expect(within(screen.getByTestId('policy-models')).getByText('Team policy')).toBeTruthy();
    expect(src('policy-budget')).toBe('own');
    expect(src('policy-downgrade')).toBe('own');
    expect(within(screen.getByTestId('policy-websearch')).getByRole('link', { name: /Budget Management/ }).getAttribute('href')).toBe('/budgets#web-search-quota');
    expect(within(screen.getByTestId('policy-budget')).getByRole('link').getAttribute('href')).toBe('/budgets?team=dev');

    const members = screen.getByTestId('team-members');
    const rows = within(members).getAllByRole('row').slice(1);
    expect(rows[0].textContent).toContain('seunghwan');
    expect(rows[0].textContent).toContain('$0.16');
    await waitFor(() => expect(rows[0].textContent).toContain('3'));
    expect(rows[0].textContent).toContain('Own');
    expect(rows[1].textContent).toContain('Leader');
    // 합성 이메일은 보이지 않고 미인증 배지 — 이름 + 역할만
    expect(rows[1].textContent).not.toContain('@unknown');
    expect(within(rows[1]).getByText(en.common.unverifiedEmail)).toBeTruthy();
    // 실제 0 은 '0' — 토큰도 요청·검색 수처럼
    const cells = within(rows[1]).getAllByRole('cell');
    expect(cells[2].textContent).toBe('0');
    expect(cells[3].textContent).toBe('0');
    // 마지막 사용 — 열 제목에 브라우저 TZ, 셀은 브라우저 TZ 시각
    expect(within(members).getByRole('columnheader', { name: `Last used (${tzZoneLabel(BROWSER_TZ)})` })).toBeTruthy();
    expect(rows[0].textContent).toContain(fmtDateMinute('2026-11-06T08:50:00Z', BROWSER_TZ));
    expect(within(members).getByText(/Last used: your browser time zone/)).toBeTruthy();
    await userEvent.setup().click(rows[0]);
    expect(onOpenUser).toHaveBeenCalledWith(U2);
  });

  it('팀장 변경 — 합성 이메일 대신 이름 + 미인증 표기, 관리자는 지정 불가', async () => {
    users.getTeamEffectivePolicyAction.mockResolvedValue(ok(policy()));
    wrap(<TeamDetail node={TEAM} breadcrumb={[]} onOpenUser={vi.fn()} onChanged={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Change leader' }));
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Team: NDS_Developers')).toBeTruthy();
    expect(within(dialog).getByText('sub-123')).toBeTruthy();
    expect(within(dialog).queryByText('sub-123@unknown')).toBeNull();
    expect(within(dialog).getByText(en.common.unverifiedEmail)).toBeTruthy();
    expect(within(dialog).getByRole('option', { name: /seunghwan/ })).toBeTruthy();
  });

  it('정책 조회 실패는 실패 문구', async () => {
    users.getTeamEffectivePolicyAction.mockResolvedValue({ success: false, error: 'x' });
    wrap(<TeamDetail node={TEAM} breadcrumb={[]} onOpenUser={vi.fn()} onChanged={vi.fn()} />);
    expect(await screen.findByText('Could not load the policies.')).toBeTruthy();
  });
});

describe('사용자 상세', () => {
  const USER = TEAM.children[1];

  it('머리·바로가기·카드·모델별·정책 출처(팀 상속 / 개별 지정)·유효 정책', async () => {
    users.getEffectivePolicyAction.mockResolvedValue(
      ok(policy({
        user_id: U2,
        email: 'seunghwan@nongshim.co.kr',
        allowed_models_source: 'team',
        rate_limits: [{ scope: 'USER', model_alias: null, rpm_limit: 30, tpm_limit: null, cpm_limit_usd: null, cph_limit_usd: null }],
      })),
    );
    users.getTeamEffectivePolicyAction.mockResolvedValue(ok(policy()));
    wrap(<UserDetail node={USER} teamId="dev" breadcrumb={['Default Organization', 'NDS', 'Developers']} onChanged={vi.fn()} />);
    expect(screen.getByTestId('user-title').textContent).toBe('seunghwan');
    expect(screen.getByText('seunghwan@nongshim.co.kr')).toBeTruthy();
    await screen.findByText(`last used ${stampZ('2026-11-06T08:50:00Z')}`, { exact: false });
    expect(screen.getByRole('link', { name: /Analytics/ }).getAttribute('href')).toBe(`/analytics?group_by=user&user=${U2}`);
    expect(screen.getByRole('link', { name: /Body Logs/ }).getAttribute('href')).toBe('/monitoring/body-logs?user=seunghwan%40nongshim.co.kr');
    expect(screen.getByRole('link', { name: /API Keys/ }).getAttribute('href')).toBe('/keys?email=seunghwan%40nongshim.co.kr');

    const kpis = screen.getByTestId('user-kpis');
    await within(kpis).findByText('$0.16');
    await within(kpis).findByText('100% of team cost');
    expect(within(kpis).getByText('$9.84')).toBeTruthy(); // 팀 예산 잔액
    expect(within(screen.getByTestId('user-models')).getByText('Claude Sonnet 5.5')).toBeTruthy();

    await screen.findByTestId('policy-models');
    const src = (id: string) => screen.getByTestId(id).querySelector('[data-source]')?.getAttribute('data-source');
    expect(src('policy-models')).toBe('team');
    expect(within(screen.getByTestId('policy-models')).getByText('Inherited from team')).toBeTruthy();
    expect(src('policy-rate')).toBe('own');
    expect(within(screen.getByTestId('policy-rate')).getByText('Set for this user')).toBeTruthy();
    expect(within(screen.getByTestId('policy-rate')).getByText('RPM 30')).toBeTruthy();
    expect(src('policy-budget')).toBe('team');

    const eff = screen.getByTestId('user-effective');
    expect(within(eff).getByTestId('blocked-chips').textContent).toContain('Claude Code · Opus 4.8');
    // 칩 툴팁은 body 로 포털(뷰포트 안 배치) — 화면 전체에서 찾는다
    expect(
      screen.getAllByRole('tooltip', { hidden: true }).some((n) => n.textContent?.includes(`Blocked by: ${en.users.effectivePolicy.axis.user_model}`)),
    ).toBe(true);
    expect(within(eff).getByText(/Claude Code · Opus 4\.8 is blocked|Opus 4\.8/, { selector: 'p[role="note"]' })).toBeTruthy();
    // 매트릭스는 기본 접힘, 펼치면 앱 열은 표시 이름
    expect(within(eff).queryByTestId('effective-matrix')).toBeNull();
    await userEvent.setup().click(within(eff).getByRole('button', { name: /Show the full model × app table/ }));
    const matrix = within(eff).getByTestId('effective-matrix');
    expect(within(matrix).getByRole('columnheader', { name: 'Claude Code' })).toBeTruthy();
    expect(within(matrix).queryByRole('columnheader', { name: 'claude-code' })).toBeNull();
  });

  it('합성 이메일 사용자 — 이름만 + 미인증 배지, 팀이 세 앱을 명시 허용하면 "(team allows all)"', async () => {
    const SYN: OrgTreeNode = {
      id: U1,
      name: 'heomk@nongshim.co.kr',
      type: 'USER',
      children: [],
      meta: meta({ email: '61739d0a-8081-7068-b73f-55872f2b0000@unknown', role: 'ADMIN', team_name: 'Developers' }),
    };
    users.getEffectivePolicyAction.mockResolvedValue(
      ok(policy({ user_id: U1, allowed_clients: ['claude-code', 'cowork', 'codex'], allowed_clients_source: 'team' })),
    );
    users.getTeamEffectivePolicyAction.mockResolvedValue(ok(policy()));
    const { container } = wrap(<UserDetail node={SYN} teamId="dev" breadcrumb={[]} onChanged={vi.fn()} />);
    expect(screen.getByTestId('user-title').textContent).toBe('heomk@nongshim.co.kr');
    expect(screen.getAllByText(en.common.unverifiedEmail).length).toBeGreaterThan(0);
    const apps = await screen.findByTestId('policy-apps');
    expect(within(apps).getByText('All 3 apps (team allows all)')).toBeTruthy();
    expect(within(apps).queryByText(/explicit/)).toBeNull();
    // 화면 어디에도 합성 주소·UUID 가 글자로 나오지 않는다(바로가기 href 는 필터라 제외)
    expect(container.textContent).not.toContain('@unknown');
    expect(container.textContent).not.toContain('61739d0a');
  });
});
