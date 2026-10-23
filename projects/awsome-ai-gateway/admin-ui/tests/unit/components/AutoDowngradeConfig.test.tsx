// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AutoDowngradeConfig — 설정 로드 실패가 "설정 없음" 폼으로 위장하지 않아야 한다.
 *
 * 배경(kiro R19-S1): 구현은 성공/실패 무관하게 setLoaded(true) 를 호출해 실패 시
 * enabled=false·rules=[] 상태의 폼이 렌더됐다. 운영자가 이 폼에서 저장하면
 * PUT(전체 교체)이 기존 규칙을 전부 지웠다 — 실패 시 폼 대신 에러+재시도를
 * 보여 저장 경로를 차단한다.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { AutoDowngradeConfig } from '@/components/budgets/AutoDowngradeConfig';
import type { ModelListItem } from '@/types/entities';

const getDowngradeConfigAction = vi.fn();
const setDowngradeConfigAction = vi.fn();
const deleteDowngradeConfigAction = vi.fn();

vi.mock('@/lib/actions/budgets', () => ({
  getDowngradeConfigAction: (...a: unknown[]) => getDowngradeConfigAction(...a),
  setDowngradeConfigAction: (...a: unknown[]) => setDowngradeConfigAction(...a),
  deleteDowngradeConfigAction: (...a: unknown[]) => deleteDowngradeConfigAction(...a),
}));

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

vi.mock('@/components/common/DowngradeDiagram', () => ({
  DowngradeDiagram: () => null,
}));

const model: ModelListItem = {
  alias: 'm1',
  provider: 'bedrock',
  model_id: 'm1',
  endpoint_url: null,
  is_active: true,
  input_price_per_1k: 0.001,
  output_price_per_1k: 0.002,
  cache_creation_5m_price_per_1k: 0,
  cache_creation_1h_price_per_1k: 0,
  cache_read_price_per_1k: 0,
  long_context_threshold_tokens: null,
  long_context_input_price_per_1k: null,
  long_context_output_price_per_1k: null,
  long_context_cache_creation_5m_price_per_1k: null,
  long_context_cache_creation_1h_price_per_1k: null,
  long_context_cache_read_price_per_1k: null,
  has_pricing: true,
  max_tokens: null,
  context_window: null,
  created_at: null,
  description: null,
  display_name: null,
  allowed_clients: null,
  account_role_arn: null,
  external_id: null,
  region: null,
};

function renderPanel() {
  return render(
    <AutoDowngradeConfig
      scopeType="TEAM"
      scopeId="team-1"
      scopeName="Team One"
      models={[model]}
    />,
  );
}

describe('AutoDowngradeConfig — 설정 로드 실패', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('실패 시 폼 대신 에러+재시도를 보이고 저장 경로가 없다', async () => {
    getDowngradeConfigAction.mockResolvedValue({
      success: false,
      error: 'boom',
    });
    renderPanel();

    await waitFor(() => expect(screen.getByText('downgradeLoadFailed')).toBeInTheDocument());
    expect(toast).toHaveBeenCalledWith(
      expect.objectContaining({ type: 'error', message: 'downgradeLoadFailed' }),
    );
    // "설정 없음" 폼(스위치·규칙 편집·저장)이 렌더되면 안 된다 — 그 상태로
    // 저장하면 기존 규칙이 전부 지워진다.
    expect(screen.queryByRole('checkbox')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'save' })).not.toBeInTheDocument();
  });

  it('재시도가 다시 로드해 성공하면 폼이 나타난다', async () => {
    getDowngradeConfigAction
      .mockResolvedValueOnce({ success: false, error: 'boom' })
      .mockResolvedValueOnce({
        success: true,
        data: { enabled: true, rules: [{ from_model_alias: 'm1', to_model_alias: 'm1', threshold_pct: 80 }] },
      });
    renderPanel();

    await waitFor(() => expect(screen.getByText('downgradeLoadFailed')).toBeInTheDocument());
    fireEvent.click(screen.getByRole('button', { name: 'retry' }));

    await waitFor(() =>
      expect(screen.getByRole('checkbox', { name: 'autoDowngrade' })).toBeInTheDocument(),
    );
    expect(getDowngradeConfigAction).toHaveBeenCalledTimes(2);
  });
});
