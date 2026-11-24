// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 정책 편집 대화상자 — 항목 하나만 저장.
 *  - 저장은 바뀐 게 있을 때만, 되돌리기 = DELETE(상속), Esc 는 저장 없이 닫는다
 *  - 앱 접근: 상속 / 모든 앱(개별) / 선택한 앱만
 *  - 허용 모델: 상속 중 저장 경고, 이름 중복·변경 배지, 팀은 전체 선택 = [](제한 없음)
 *  - Rate limit: 단위, 비우면 적용되는 값(팀 → 조직), 검증, 모두 비우고 저장 = DELETE
 */

import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import en from '../../../messages/en.json';
import { AppAccessDialog } from '@/components/users/detail/AppAccessDialog';
import { AllowedModelsDialog } from '@/components/users/detail/AllowedModelsDialog';
import { RateLimitDialog } from '@/components/users/detail/RateLimitDialog';
import type { ModelListItem } from '@/types/entities';

const { users, models, rate, toast } = vi.hoisted(() => ({
  users: {
    getUserAllowedClientsAction: vi.fn(),
    setUserAllowedClientsAction: vi.fn(),
    getScopeAllowedClientsAction: vi.fn(),
    setScopeAllowedClientsAction: vi.fn(),
    getUserAllowedModelsAction: vi.fn(),
    setUserAllowedModelsAction: vi.fn(),
  },
  models: { getTeamAllowedModelsAction: vi.fn(), setTeamAllowedModelsAction: vi.fn() },
  rate: { getRateLimitStatusAction: vi.fn(), setRateLimitAction: vi.fn(), deleteRateLimitAction: vi.fn() },
  toast: vi.fn(),
}));
vi.mock('@/lib/actions/users', () => users);
vi.mock('@/lib/actions/models', () => models);
vi.mock('@/lib/actions/rate-limits', () => rate);
vi.mock('@/components/common/ToastProvider', () => ({ useToast: () => ({ toast }) }));

const ok = <T,>(data: T) => ({ success: true as const, data });

beforeEach(() => {
  Object.values({ ...users, ...models, ...rate }).forEach((f) => f.mockReset());
  toast.mockReset();
});

function wrap(node: ReactNode) {
  return render(
    <NextIntlClientProvider locale="en" messages={en as unknown as AbstractIntlMessages}>
      {node}
    </NextIntlClientProvider>,
  );
}

const saveBtn = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;

describe('앱 접근 대화상자', () => {
  it('상속 중 — 저장은 바뀐 뒤에만, 선택한 앱만 → PUT 그 목록, 닫히고 onSaved', async () => {
    users.getUserAllowedClientsAction.mockResolvedValue(ok({ clients: [] }));
    users.setUserAllowedClientsAction.mockResolvedValue(ok({ clients: ['codex'] }));
    const onSaved = vi.fn();
    const onClose = vi.fn();
    wrap(
      <AppAccessDialog isOpen onClose={onClose} scope="user" id="u1" target="Kim · NDS_Developers" inheritedText="No restriction · all 3 apps" onSaved={onSaved} />,
    );
    const dialog = await screen.findByRole('dialog');
    expect(within(dialog).getByText('Target: Kim · NDS_Developers')).toBeTruthy();
    await waitFor(() => expect((within(dialog).getByRole('radio', { name: /Follow team policy/ }) as HTMLInputElement).checked).toBe(true));
    expect(saveBtn().disabled).toBe(true);
    expect(within(dialog).getByTestId('policy-dialog-summary').textContent).toBe('No changes');
    // 상속 중이면 되돌리기 비활성
    expect((within(dialog).getByRole('button', { name: 'Revert to inherited' }) as HTMLButtonElement).disabled).toBe(true);

    const user = userEvent.setup();
    await user.click(within(dialog).getByRole('radio', { name: /Only selected apps/ }));
    expect(saveBtn().disabled).toBe(true); // 아직 고른 앱 없음
    expect(within(dialog).getByRole('alert').textContent).toContain('Pick at least one app');
    await user.click(within(dialog).getByRole('checkbox', { name: 'Codex' }));
    expect(saveBtn().disabled).toBe(false);
    expect(within(dialog).getByTestId('policy-dialog-summary').textContent).toBe('Saving allows: Codex');
    await user.click(saveBtn());
    await waitFor(() => expect(users.setUserAllowedClientsAction).toHaveBeenCalledWith('u1', ['codex']));
    expect(onSaved).toHaveBeenCalled();
    expect(onClose).toHaveBeenCalled();
  });

  it('개별 정책이 있으면 되돌리기 = 빈 목록 저장(DELETE)', async () => {
    users.getScopeAllowedClientsAction.mockResolvedValue(ok({ clients: ['claude-code', 'cowork', 'codex'] }));
    users.setScopeAllowedClientsAction.mockResolvedValue(ok({ clients: [] }));
    wrap(<AppAccessDialog isOpen onClose={vi.fn()} scope="team" id="t1" target="NDS_Developers" inheritedText="org" onSaved={vi.fn()} />);
    const dialog = await screen.findByRole('dialog');
    await waitFor(() => expect((within(dialog).getByRole('radio', { name: /Allow all apps/ }) as HTMLInputElement).checked).toBe(true));
    await userEvent.setup().click(within(dialog).getByRole('button', { name: 'Revert to inherited' }));
    await waitFor(() => expect(users.setScopeAllowedClientsAction).toHaveBeenCalledWith('team', 't1', []));
  });

  it('Esc 는 저장 없이 닫는다', async () => {
    users.getUserAllowedClientsAction.mockResolvedValue(ok({ clients: [] }));
    const onClose = vi.fn();
    wrap(<AppAccessDialog isOpen onClose={onClose} scope="user" id="u1" target="Kim" inheritedText="x" onSaved={vi.fn()} />);
    const dialog = await screen.findByRole('dialog');
    await userEvent.setup().click(within(dialog).getByRole('radio', { name: /Allow all apps/ }));
    fireEvent.keyDown(dialog, { key: 'Escape' });
    expect(onClose).toHaveBeenCalled();
    expect(users.setUserAllowedClientsAction).not.toHaveBeenCalled();
  });
});

const cat = [
  { alias: 'claude-opus-4-8', display_name: 'Claude Code · Opus 4.8', model_id: 'global.anthropic.claude-opus-4-8' },
  { alias: 'global.anthropic.claude-opus-4-8', display_name: 'Claude Code · Opus 4.8', model_id: 'global.anthropic.claude-opus-4-8' },
  { alias: 'claude-haiku-5-5', display_name: 'Claude Haiku 5.5', model_id: 'h' },
] as unknown as ModelListItem[];

describe('허용 모델 대화상자', () => {
  it('상속 중 — 경고, 이름 중복 배지, 변경 배지·요약, 저장은 전체 목록 PUT', async () => {
    users.getUserAllowedModelsAction.mockResolvedValue(ok({ modelAliases: [] }));
    users.setUserAllowedModelsAction.mockResolvedValue(ok({ modelAliases: [] }));
    wrap(
      <AllowedModelsDialog
        isOpen
        onClose={vi.fn()}
        scope="user"
        id="u1"
        target="Kim"
        catalog={cat}
        inherited={['global.anthropic.claude-opus-4-8', 'claude-haiku-5-5']}
        inheriting
        onSaved={vi.fn()}
      />,
    );
    const dialog = await screen.findByRole('dialog');
    expect(await within(dialog).findByText(/copies the whole list as this user's own policy/)).toBeTruthy();
    expect(within(dialog).getAllByText('Duplicate name')).toHaveLength(2);
    expect(within(dialog).getByTestId('policy-dialog-summary').textContent).toBe('2 of 3 allowed · no changes');
    expect(saveBtn().disabled).toBe(true);

    const opus = within(dialog).getByRole('checkbox', { name: 'Claude Code · Opus 4.8 (claude-opus-4-8)' });
    expect((opus as HTMLInputElement).checked).toBe(false);
    await userEvent.setup().click(opus);
    expect(within(dialog).getByText('Changed')).toBeTruthy();
    expect(within(dialog).getByTestId('policy-dialog-summary').textContent).toBe('3 of 3 allowed · 1 changed');
    await userEvent.setup().click(saveBtn());
    await waitFor(() =>
      expect(users.setUserAllowedModelsAction).toHaveBeenCalledWith('u1', [
        'claude-opus-4-8',
        'global.anthropic.claude-opus-4-8',
        'claude-haiku-5-5',
      ]),
    );
  });

  it('필터·검색, 그룹 전체 해제 후엔 저장 불가', async () => {
    models.getTeamAllowedModelsAction.mockResolvedValue(ok({ team_id: 't1', model_aliases: [] }));
    wrap(<AllowedModelsDialog isOpen onClose={vi.fn()} scope="team" id="t1" target="Dev" catalog={cat} onSaved={vi.fn()} />);
    const dialog = await screen.findByRole('dialog');
    const user = userEvent.setup();
    await within(dialog).findByText('All 3');
    await user.type(within(dialog).getByRole('searchbox', { name: 'Model name or ID' }), 'haiku');
    expect(within(dialog).getAllByRole('checkbox')).toHaveLength(1);
    await user.clear(within(dialog).getByRole('searchbox', { name: 'Model name or ID' }));
    for (const b of within(dialog).getAllByRole('button', { name: 'Clear all' })) await user.click(b);
    expect(saveBtn().disabled).toBe(true);
    expect(within(dialog).getByRole('alert').textContent).toContain('Allow at least one model');
  });

  it('팀 — 전체 선택 저장은 [](제한 없음), 일부는 그 목록', async () => {
    models.getTeamAllowedModelsAction.mockResolvedValue(ok({ team_id: 't1', model_aliases: ['claude-haiku-5-5'] }));
    models.setTeamAllowedModelsAction.mockResolvedValue(ok({ team_id: 't1', model_aliases: [] }));
    wrap(<AllowedModelsDialog isOpen onClose={vi.fn()} scope="team" id="t1" target="Dev" catalog={cat} onSaved={vi.fn()} />);
    const dialog = await screen.findByRole('dialog');
    const user = userEvent.setup();
    await within(dialog).findByText('1 of 3 allowed · no changes');
    await user.click(within(dialog).getByRole('button', { name: 'Select all' }));
    await user.click(saveBtn());
    await waitFor(() => expect(models.setTeamAllowedModelsAction).toHaveBeenCalledWith('t1', []));
  });
});

describe('Rate limit 대화상자', () => {
  it('단위·상속 값, 검증, 저장 값', async () => {
    rate.getRateLimitStatusAction.mockResolvedValue(ok({ own: null, inherited: null, inherited_scope: null }));
    rate.setRateLimitAction.mockResolvedValue(ok(undefined));
    wrap(
      <RateLimitDialog
        isOpen
        onClose={vi.fn()}
        scope="user"
        id="u1"
        target="Kim"
        team={{ rpm: 60, tpm: null, cpm: null, cph: null }}
        org={null}
        onSaved={vi.fn()}
      />,
    );
    const dialog = await screen.findByRole('dialog');
    await within(dialog).findByText('req/min');
    expect(within(dialog).getByText('tokens/min')).toBeTruthy();
    expect(within(dialog).getByText('$/min')).toBeTruthy();
    expect(within(dialog).getByText('$/hour')).toBeTruthy();
    expect(within(dialog).getByText('team: 60 → org: none = 60')).toBeTruthy();
    expect(within(dialog).getAllByText('team: none → org: none = unlimited')).toHaveLength(3);
    const user = userEvent.setup();
    const rpm = within(dialog).getByLabelText('RPM');
    await user.type(rpm, '0');
    expect(saveBtn().disabled).toBe(true);
    expect(within(dialog).getByRole('alert').textContent).toBe('Whole number above 0.');
    await user.clear(rpm);
    await user.type(rpm, '30');
    await user.type(within(dialog).getByLabelText('CPH'), '2.5');
    expect(within(dialog).getByTestId('policy-dialog-summary').textContent).toBe('2 items changed');
    await user.click(saveBtn());
    await waitFor(() =>
      expect(rate.setRateLimitAction).toHaveBeenCalledWith({ target_id: 'u1', scope: 'USER', rpm: 30, tpm: null, cpm: null, cph: 2.5 }),
    );
  });

  it('모두 비우고 저장 = DELETE, 되돌리기도 DELETE', async () => {
    rate.getRateLimitStatusAction.mockResolvedValue(ok({ own: { rpm: 10, tpm: null, cpm: null, cph: null }, inherited: null, inherited_scope: null }));
    rate.deleteRateLimitAction.mockResolvedValue(ok(undefined));
    wrap(<RateLimitDialog isOpen onClose={vi.fn()} scope="team" id="t1" target="Dev" org={null} onSaved={vi.fn()} />);
    const dialog = await screen.findByRole('dialog');
    const rpm = (await within(dialog).findByLabelText('RPM')) as HTMLInputElement;
    expect(rpm.value).toBe('10');
    expect(within(dialog).getAllByText('org: none = unlimited')).toHaveLength(4);
    await userEvent.setup().clear(rpm);
    expect(within(dialog).getByTestId('policy-dialog-summary').textContent).toBe("Saving removes this team's limits");
    await userEvent.setup().click(saveBtn());
    await waitFor(() => expect(rate.deleteRateLimitAction).toHaveBeenCalledWith('team', 't1'));
  });
});
