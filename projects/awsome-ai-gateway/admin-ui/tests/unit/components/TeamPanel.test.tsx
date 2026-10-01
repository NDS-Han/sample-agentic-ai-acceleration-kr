// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * TeamPanel — T14 팀 유효 정책 카드 + T15 통합 Apply 섹션별 결과.
 *
 * T14: TEAM 노드를 열면 `GET /admin/teams/{id}/effective-policy` 결과가
 *      읽기 전용 카드(모델×앱 매트릭스)로 렌더돼야 한다.
 * T15: Apply 는 dirty 섹션을 **전부** 시도한다 — 앞 섹션 실패가 뒤 섹션의
 *      저장을 막으면 안 되고(독립 리소스), sticky 스트립에 섹션별
 *      저장됨/실패가 구분돼 보여야 한다. 실패한 섹션은 dirty 를 유지한다.
 *
 * 자식 패널(앱 접근/모델/레이트리밋)은 save/revert 계약만 중요하므로
 * ref 핸들 스텁으로 대체한다.
 */

import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { OrgDetailPanel } from '@/components/users/OrgDetailPanel';
import { getTeamEffectivePolicyAction } from '@/lib/actions/users';
import { listActiveModelsAction } from '@/lib/actions/models';
import type { EffectivePolicy, OrgTreeNode } from '@/types/entities';

vi.mock('next-intl', () => {
  const tFn = (key: string, params?: Record<string, unknown>) =>
    params && 'count' in params ? `${key}=${params.count}` : key;
  return { useTranslations: () => tFn };
});

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: vi.fn(), refresh: vi.fn() }),
}));

vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock('@/lib/actions/models', () => ({
  listActiveModelsAction: vi.fn(),
}));

vi.mock('@/lib/actions/users', () => ({
  getTeamEffectivePolicyAction: vi.fn(),
  forceReauthTeamAction: vi.fn(),
  setTeamLeaderAction: vi.fn(),
  unsetTeamLeaderAction: vi.fn(),
}));

/** 각 자식 패널의 save() 결과와 호출 순서를 제어한다. */
const saveCtl = vi.hoisted(() => ({
  calls: [] as string[],
  results: { apps: true, models: true, ratelimit: true },
}));

interface StubPanelProps {
  onDirtyChange?: (dirty: boolean) => void;
}

/** ref.save()/revert() 계약을 가진 자식 패널 스텁을 만든다. */
vi.mock('@/components/users/ScopeAppAccessPanel', async () => {
  const React = await import('react');
  const Stub = React.forwardRef(function Stub(
    props: StubPanelProps,
    ref: React.Ref<{ save: () => Promise<boolean>; revert: () => void }>,
  ) {
    React.useImperativeHandle(ref, () => ({
      save: async () => {
        saveCtl.calls.push('apps');
        // 실제 패널은 저장 성공 시 baseline 갱신 → dirty 해소 보고.
        if (saveCtl.results.apps) props.onDirtyChange?.(false);
        return saveCtl.results.apps;
      },
      revert: () => props.onDirtyChange?.(false),
    }));
    return <button data-testid="edit-apps" onClick={() => props.onDirtyChange?.(true)} />;
  });
  return { ScopeAppAccessPanel: Stub };
});

vi.mock('@/components/models/TeamModelPermissionPanel', async () => {
  const React = await import('react');
  const Stub = React.forwardRef(function Stub(
    props: StubPanelProps,
    ref: React.Ref<{ save: () => Promise<boolean>; revert: () => void }>,
  ) {
    React.useImperativeHandle(ref, () => ({
      save: async () => {
        saveCtl.calls.push('models');
        if (saveCtl.results.models) props.onDirtyChange?.(false);
        return saveCtl.results.models;
      },
      revert: () => props.onDirtyChange?.(false),
    }));
    return <button data-testid="edit-models" onClick={() => props.onDirtyChange?.(true)} />;
  });
  return { TeamModelPermissionPanel: Stub };
});

vi.mock('@/components/users/ScopeRateLimitPanel', async () => {
  const React = await import('react');
  const Stub = React.forwardRef(function Stub(
    props: StubPanelProps,
    ref: React.Ref<{ save: () => Promise<boolean>; revert: () => void }>,
  ) {
    React.useImperativeHandle(ref, () => ({
      save: async () => {
        saveCtl.calls.push('ratelimit');
        if (saveCtl.results.ratelimit) props.onDirtyChange?.(false);
        return saveCtl.results.ratelimit;
      },
      revert: () => props.onDirtyChange?.(false),
    }));
    return <button data-testid="edit-rl" onClick={() => props.onDirtyChange?.(true)} />;
  });
  return { ScopeRateLimitPanel: Stub };
});

const TEAM_NODE = {
  id: 'team-1',
  name: 'Developers',
  type: 'TEAM',
  children: [],
  meta: {
    member_count: 0,
    team_count: null,
    leader_name: null,
    leader_user_id: null,
    email: null,
    role: null,
    team_name: null,
  },
} as OrgTreeNode;

const TEAM_POLICY: EffectivePolicy = {
  user_id: null,
  email: null,
  team_id: 'team-1',
  team_name: 'Developers',
  allowed_clients: ['claude-code'],
  allowed_clients_source: 'team',
  allowed_models: ['claude-sonnet'],
  allowed_models_source: 'team',
  web_search: { 'claude-code': true, cowork: false },
  cells: [
    { client: 'claude-code', model_alias: 'claude-sonnet', allowed: true, blocked_by: [] },
    { client: 'cowork', model_alias: 'claude-sonnet', allowed: false, blocked_by: ['user_app'] },
  ],
  budgets: [],
  rate_limits: [
    {
      scope: 'TEAM',
      model_alias: null,
      rpm_limit: 60,
      tpm_limit: null,
      cpm_limit_usd: null,
      cph_limit_usd: null,
    },
  ],
  downgrade_rules: [],
};

describe('TeamPanel — 팀 유효 정책 (T14)', () => {
  beforeEach(() => {
    vi.mocked(listActiveModelsAction).mockResolvedValue({ success: true, data: [] });
    vi.mocked(getTeamEffectivePolicyAction).mockResolvedValue({
      success: true,
      data: TEAM_POLICY,
    });
  });

  it('팀 노드를 열면 팀 effective-policy 엔드포인트로 조회한다', async () => {
    render(<OrgDetailPanel node={TEAM_NODE} />);
    await screen.findByLabelText(/axis\.user_app/);
    expect(getTeamEffectivePolicyAction).toHaveBeenCalledWith('team-1');
  });

  it('매트릭스의 거부 셀에 막힌 축이 aria-label 로 표시된다', async () => {
    render(<OrgDetailPanel node={TEAM_NODE} />);
    // cowork 셀은 user_app 축에서 거부 — 팀 뷰에서도 같은 카드/축 라벨을 쓴다.
    const denied = await screen.findByLabelText(/axis\.user_app/);
    expect(denied).toBeInTheDocument();
  });

  it('조회 실패 시 loadFailed 상태를 보여준다', async () => {
    vi.mocked(getTeamEffectivePolicyAction).mockResolvedValue({
      success: false,
      error: 'boom',
    });
    render(<OrgDetailPanel node={TEAM_NODE} />);
    await screen.findByText('loadFailed');
  });
});

describe('TeamPanel — 통합 Apply 섹션별 결과 (T15)', () => {
  beforeEach(() => {
    saveCtl.calls.length = 0;
    saveCtl.results = { apps: true, models: true, ratelimit: true };
    vi.mocked(listActiveModelsAction).mockResolvedValue({ success: true, data: [] });
    vi.mocked(getTeamEffectivePolicyAction).mockResolvedValue({
      success: true,
      data: TEAM_POLICY,
    });
  });

  async function dirtyAllAndApply() {
    fireEvent.click(await screen.findByTestId('edit-apps'));
    fireEvent.click(await screen.findByTestId('edit-models'));
    fireEvent.click(await screen.findByTestId('edit-rl'));
    fireEvent.click(await screen.findByRole('button', { name: 'apply' }));
  }

  it('앞 섹션이 실패해도 나머지 dirty 섹션을 전부 시도한다', async () => {
    saveCtl.results.apps = false;
    render(<OrgDetailPanel node={TEAM_NODE} />);
    await dirtyAllAndApply();
    await waitFor(() =>
      expect(saveCtl.calls).toEqual(['apps', 'models', 'ratelimit']),
    );
  });

  it('부분 실패 시 섹션별 저장됨/실패 칩을 보여주고 실패 섹션은 dirty 로 남는다', async () => {
    saveCtl.results.models = false;
    render(<OrgDetailPanel node={TEAM_NODE} />);
    await dirtyAllAndApply();
    // sticky 스트립: 실패 섹션 + 성공 섹션 각각 표기
    await screen.findByText(/failed: teamModelAccess/);
    expect(screen.getByText(/saved: scopeAppAccess.title/)).toBeInTheDocument();
    expect(screen.getByText(/saved: rateLimit.title/)).toBeInTheDocument();
    // 실패한 모델 섹션만 dirty 잔존 → Apply 바는 계속 뜬다
    expect(await screen.findByRole('button', { name: 'apply' })).toBeInTheDocument();
  });

  it('전부 성공하면 Apply 바가 사라진다', async () => {
    render(<OrgDetailPanel node={TEAM_NODE} />);
    await dirtyAllAndApply();
    await waitFor(() =>
      expect(screen.queryByRole('button', { name: 'apply' })).not.toBeInTheDocument(),
    );
    // 성공분이 있으므로 팀 유효 정책을 재조회한다 (초기 로드 + 저장 후).
    expect(vi.mocked(getTeamEffectivePolicyAction).mock.calls.length).toBeGreaterThanOrEqual(2);
  });

  it('초기 유효 정책 로드 실패 후 저장 성공 시 카드가 loadFailed 에서 복구된다', async () => {
    // 회귀: 재조회 성공이 teamPolicyFailed 를 리셋하지 않으면 유효한 데이터를
    // 쥐고도 카드가 에러 화면에 고착됐다.
    vi.mocked(getTeamEffectivePolicyAction)
      .mockResolvedValueOnce({ success: false, error: 'boom' })
      .mockResolvedValue({ success: true, data: TEAM_POLICY });
    render(<OrgDetailPanel node={TEAM_NODE} />);
    await screen.findByText('loadFailed');
    await dirtyAllAndApply();
    await screen.findByLabelText(/axis\.user_app/);
    expect(screen.queryByText('loadFailed')).not.toBeInTheDocument();
  });
});
