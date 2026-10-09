// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 삭제 다이얼로그 — 파괴 동작의 안전장치가 실제로 걸려 있는지 검증.
 *
 *   1. 열리면 deletion-impact 를 조회해 함께 지워지는 행 수를 보여준다.
 *   2. alias 를 정확히 타이핑해야 삭제 버튼이 열린다(실수 클릭 방지).
 *   3. downgrade 목적지로 쓰이면(blocked) alias 를 쳐도 삭제할 수 없다.
 *   4. 409 등 서버 에러는 토스트가 아니라 다이얼로그 안에 보인다.
 */

import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { DeleteModelDialog } from '@/components/models/DeleteModelDialog';
import type { ModelListItem } from '@/types/entities';
import type { ModelDeletionImpact } from '@/lib/actions/models';

const getModelDeletionImpactAction = vi.fn();
const deleteModelAction = vi.fn();

vi.mock('@/lib/actions/models', () => ({
  getModelDeletionImpactAction: (...a: unknown[]) => getModelDeletionImpactAction(...a),
  deleteModelAction: (...a: unknown[]) => deleteModelAction(...a),
}));

// next-intl 은 메시지 provider 없이는 throw — 키를 그대로 돌려주는 대역.
vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

const model: ModelListItem = {
  alias: 'legacy-claude',
  provider: 'BEDROCK',
  model_id: 'anthropic.claude-x',
  endpoint_url: null,
  is_active: false,
  input_price_per_1k: 0.003,
  output_price_per_1k: 0.015,
  cache_creation_5m_price_per_1k: 0,
  cache_creation_1h_price_per_1k: 0,
  cache_read_price_per_1k: 0,
  has_pricing: true,
  max_tokens: null,
  context_window: null,
  created_at: '2026-10-01T00:00:00Z',
  description: null,
  display_name: 'Legacy Claude',
  allowed_clients: null,
};

function makeImpact(overrides: Partial<ModelDeletionImpact> = {}): ModelDeletionImpact {
  return {
    alias: 'legacy-claude',
    usage_logs: 1234,
    pricings: 2,
    team_allowed: 3,
    user_allowed: 1,
    rate_limits: 0,
    downgrade_from: 1,
    downgrade_to: 0,
    blocked: false,
    ...overrides,
  };
}

async function renderOpen(impact: ModelDeletionImpact = makeImpact()) {
  getModelDeletionImpactAction.mockResolvedValue({ success: true, data: impact });
  const onClose = vi.fn();
  render(<DeleteModelDialog isOpen onClose={onClose} model={model} />);
  // impact 가 로드돼 카운트가 보일 때까지 기다린다.
  await waitFor(() => expect(screen.getByText('1,234')).toBeInTheDocument());
  return onClose;
}

describe('DeleteModelDialog', () => {
  beforeEach(() => {
    getModelDeletionImpactAction.mockReset();
    deleteModelAction.mockReset();
    toast.mockReset();
  });

  it('열리면 삭제 영향도를 조회해 카운트를 보여준다', async () => {
    await renderOpen();
    expect(getModelDeletionImpactAction).toHaveBeenCalledWith('legacy-claude');
    // 사용 이력은 '보존'으로 표시된다.
    expect(screen.getByText('deleteImpactUsageLogs')).toBeInTheDocument();
    expect(screen.getByText('deleteImpactRetained')).toBeInTheDocument();
  });

  it('alias 를 정확히 입력해야 삭제 버튼이 활성화된다', async () => {
    await renderOpen();
    const submit = screen.getByRole('button', { name: 'deleteButton' });
    expect(submit).toBeDisabled();

    const input = screen.getByLabelText('deleteTypeAlias');
    fireEvent.change(input, { target: { value: 'legacy' } });
    expect(submit).toBeDisabled();

    fireEvent.change(input, { target: { value: 'legacy-claude' } });
    expect(submit).toBeEnabled();
  });

  it('대소문자가 다른 alias 는 활성화하지 않는다 (exact match)', async () => {
    await renderOpen();
    const submit = screen.getByRole('button', { name: 'deleteButton' });
    fireEvent.change(screen.getByLabelText('deleteTypeAlias'), {
      target: { value: 'Legacy-Claude' },
    });
    expect(submit).toBeDisabled();
  });

  it('다운그레이드 목적지이면 alias 를 입력해도 삭제할 수 없다', async () => {
    await renderOpen(makeImpact({ downgrade_to: 2, blocked: true }));
    const submit = screen.getByRole('button', { name: 'deleteButton' });
    // 차단 안내가 보이고 입력란 자체가 잠긴다.
    expect(screen.getByText('deleteBlockedDowngradeTo')).toBeInTheDocument();
    expect(screen.getByLabelText('deleteTypeAlias')).toBeDisabled();
    expect(submit).toBeDisabled();
  });

  it('성공하면 deleteModelAction 을 호출하고 토스트 후 닫는다', async () => {
    deleteModelAction.mockResolvedValue({ success: true, data: makeImpact() });
    const onClose = await renderOpen();

    fireEvent.change(screen.getByLabelText('deleteTypeAlias'), {
      target: { value: 'legacy-claude' },
    });
    fireEvent.submit(screen.getByRole('button', { name: 'deleteButton' }).closest('form')!);

    await waitFor(() => {
      expect(deleteModelAction).toHaveBeenCalledWith('legacy-claude');
      expect(toast).toHaveBeenCalledWith(expect.objectContaining({ type: 'success' }));
      expect(onClose).toHaveBeenCalled();
    });
  });

  it('서버 에러(409 등)는 다이얼로그 안에 표시하고 닫지 않는다', async () => {
    deleteModelAction.mockResolvedValue({
      success: false,
      error: "모델 'legacy-claude' 은(는) 다운그레이드 정책의 전환 목적지입니다",
    });
    const onClose = await renderOpen();

    fireEvent.change(screen.getByLabelText('deleteTypeAlias'), {
      target: { value: 'legacy-claude' },
    });
    fireEvent.submit(screen.getByRole('button', { name: 'deleteButton' }).closest('form')!);

    await waitFor(() => {
      const alerts = screen.getAllByRole('alert').map((n) => n.textContent ?? '').join(' | ');
      expect(alerts).toContain('전환 목적지입니다');
    });
    expect(onClose).not.toHaveBeenCalled();
    expect(toast).not.toHaveBeenCalled();
  });

  it('영향도 조회 실패 시 에러를 보이고 삭제할 수 없다', async () => {
    getModelDeletionImpactAction.mockResolvedValue({
      success: false,
      error: 'impact fetch failed',
    });
    render(<DeleteModelDialog isOpen onClose={() => {}} model={model} />);

    await waitFor(() => {
      const alerts = screen.getAllByRole('alert').map((n) => n.textContent ?? '').join(' | ');
      expect(alerts).toContain('impact fetch failed');
    });
    fireEvent.change(screen.getByLabelText('deleteTypeAlias'), {
      target: { value: 'legacy-claude' },
    });
    expect(screen.getByRole('button', { name: 'deleteButton' })).toBeDisabled();
  });

  it('삭제 관련 i18n 키가 ko/en 양쪽에 있다', () => {
    const keys = [
      'deleteButton',
      'deleteConfirmTitle',
      'deleteTarget',
      'deleteWarningTitle',
      'deleteWarningBody',
      'deleteImpactLoading',
      'deleteImpactUsageLogs',
      'deleteImpactRetained',
      'deleteImpactRemoved',
      'deleteImpactPricings',
      'deleteImpactTeamAllowed',
      'deleteImpactUserAllowed',
      'deleteImpactRateLimits',
      'deleteImpactDowngradeFrom',
      'deleteBlockedDowngradeTo',
      'deleteTypeAlias',
      'deleteSuccess',
    ];
    for (const locale of ['ko', 'en']) {
      const messages = JSON.parse(
        readFileSync(path.join(process.cwd(), 'messages', `${locale}.json`), 'utf8')
      );
      for (const key of keys) {
        expect(typeof messages.models[key], `${locale}.models.${key}`).toBe('string');
      }
    }
  });
});
