// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { KeysListView } from '@/components/keys/KeysListView';
import { KeysTable } from '@/components/keys/KeysTable';
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
  useIsClient: () => true,
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

describe('KeysTable status / revoke 노출', () => {
  const base = { user_email: 'a@b.c', created_at: '2026-01-01T00:00:00Z' };
  const future = new Date(Date.now() + 365 * 24 * 3600 * 1000).toISOString();
  const past = new Date(Date.now() - 24 * 3600 * 1000).toISOString();

  it('생성·만료일은 리포팅 TZ 날짜 — 표 아래 캡션 한 번', () => {
    const key = { ...base, key_id: 'k-c', key_prefix: 'sk-c', status: KeyStatus.ACTIVE, expires_at: null } as unknown as VirtualKeyListItem;
    render(<KeysTable keys={[key]} />);
    // 2026-01-01T00:00Z 는 Asia/Seoul 로 01-01 09:00 — 날짜 그대로.
    expect(screen.getByText('2026-01-01')).toBeInTheDocument();
    expect(screen.getAllByText(/^dateOnly:.*Asia\/Seoul/)).toHaveLength(1);
  });

  it('EXPIRED + 만료일 미래 → 교체됨 배지 + 툴팁, Revoke 없음', () => {
    const key = { ...base, key_id: 'k-s', key_prefix: 'sk-sup', status: KeyStatus.EXPIRED, expires_at: future } as VirtualKeyListItem;
    render(<KeysTable keys={[key]} />);
    expect(screen.getAllByText('keyStatus.SUPERSEDED').length).toBeGreaterThan(0);
    expect(screen.queryByText('keyStatus.EXPIRED')).not.toBeInTheDocument();
    expect(screen.getByRole('tooltip')).toHaveTextContent('supersededHint');
    expect(screen.queryByRole('button', { name: 'revoke' })).not.toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('EXPIRED + 만료일 과거 → 만료됨 그대로, Revoke 없음', () => {
    const key = { ...base, key_id: 'k-e', key_prefix: 'sk-exp', status: KeyStatus.EXPIRED, expires_at: past } as VirtualKeyListItem;
    render(<KeysTable keys={[key]} />);
    expect(screen.getByText('keyStatus.EXPIRED')).toBeInTheDocument();
    expect(screen.queryByText('keyStatus.SUPERSEDED')).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'revoke' })).not.toBeInTheDocument();
  });

  it('REVOKED 는 만료일이 미래여도 폐기됨, Revoke 없음', () => {
    const key = { ...base, key_id: 'k-r', key_prefix: 'sk-rev', status: KeyStatus.REVOKED, expires_at: future } as VirtualKeyListItem;
    render(<KeysTable keys={[key]} />);
    expect(screen.getByText('keyStatus.REVOKED')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'revoke' })).not.toBeInTheDocument();
    expect(screen.getByText('—')).toBeInTheDocument();
  });

  it('ACTIVE 는 Revoke 버튼이 활성 상태로 보인다', () => {
    render(<KeysTable keys={[ACTIVE_KEY]} />);
    expect(screen.getByRole('button', { name: 'revoke' })).toBeEnabled();
  });
});
