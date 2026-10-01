// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { KeysListView } from '@/components/keys/KeysListView';
import { revokeKeyAction } from '@/lib/actions/keys';
import type { VirtualKeyListItem } from '@/types/entities';
import { KeyStatus } from '@/types/enums';

vi.mock('@/lib/actions/keys', () => ({
  revokeKeyAction: vi.fn(),
  listKeysAction: vi.fn(),
}));

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string, params?: Record<string, unknown>) =>
    params ? `${key}:${JSON.stringify(params)}` : key,
}));

vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast: vi.fn() }),
}));

vi.mock('@/components/common/ReportingTimezoneProvider', () => ({
  useReportingTz: () => 'Asia/Seoul',
}));

const ACTIVE_KEY: VirtualKeyListItem = {
  key_id: 'k-1',
  key_prefix: 'sk-abc',
  user_email: 'a@b.c',
  status: KeyStatus.ACTIVE,
  created_at: '2026-01-01T00:00:00Z',
  expires_at: null,
} as VirtualKeyListItem;

describe('KeysListView revoke', () => {
  beforeEach(() => vi.clearAllMocks());

  it('revoke 성공 시 행을 목록에서 제거한다 (ACTIVE 필터 유령 행 방지)', async () => {
    vi.mocked(revokeKeyAction).mockResolvedValue({ success: true, data: undefined });
    render(
      <KeysListView
        initialItems={[ACTIVE_KEY]}
        initialCursor={null}
        hasMore={false}
        email=""
        status="ACTIVE"
        limit={50}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'revoke' }));
    // ConfirmDialog 확인
    await userEvent.click(await screen.findByRole('button', { name: 'revokeDialogConfirm' }));
    await waitFor(() => {
      expect(screen.queryByText('sk-abc')).not.toBeInTheDocument();
    });
  });

  it('revoke 실패 시 행이 남는다', async () => {
    vi.mocked(revokeKeyAction).mockResolvedValue({ success: false, error: 'err' });
    render(
      <KeysListView
        initialItems={[ACTIVE_KEY]}
        initialCursor={null}
        hasMore={false}
        email=""
        status="ACTIVE"
        limit={50}
      />,
    );
    await userEvent.click(screen.getByRole('button', { name: 'revoke' }));
    await userEvent.click(await screen.findByRole('button', { name: 'revokeDialogConfirm' }));
    await waitFor(() => {
      expect(revokeKeyAction).toHaveBeenCalledWith('k-1');
    });
    expect(screen.getByText('sk-abc')).toBeInTheDocument();
  });
});
