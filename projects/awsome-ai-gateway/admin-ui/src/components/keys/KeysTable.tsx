'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState } from 'react';
import { useTranslations } from 'next-intl';
import Link from 'next/link';
import { revokeKeyAction } from '@/lib/actions/keys';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDate } from '@/lib/utils/format';
import type { VirtualKeyListItem } from '@/types/entities';
import { KeyStatus } from '@/types/enums';

interface KeysTableProps {
  keys: VirtualKeyListItem[];
  /** revoke 성공 시 호출 — 부모가 items state 에서 행을 제거한다.
      (revalidatePath 로 서버 컴포넌트는 갱신돼도 KeysListView 의
      useState(initialItems) 는 유지되므로 로컬 동기화가 필요하다.) */
  onRevoked?: (_keyId: string) => void;
}

const STATUS_TONE: Record<string, BadgeTone> = {
  [KeyStatus.ACTIVE]: 'teal',
  [KeyStatus.EXPIRED]: 'neutral',
  [KeyStatus.REVOKED]: 'pink',
};

function formatDate(iso: string | null, noExpiry: string, timeZone: string): string {
  if (!iso) return noExpiry;
  // 리포팅 TZ 기준 — 만료일이 브라우저 로컬 기준 하루 어긋나 보이는 것 방지.
  return fmtDate(iso, timeZone);
}

export function KeysTable({ keys, onRevoked }: KeysTableProps) {
  const t = useTranslations('keys');
  const tz = useReportingTz();
  const { toast } = useToast();

  const [revokeState, setRevokeState] = useState<{
    isOpen: boolean;
    keyId: string;
    keyPrefix: string;
  }>({ isOpen: false, keyId: '', keyPrefix: '' });

  const [revokingId, setRevokingId] = useState<string | null>(null);

  const handleRevoke = async () => {
    setRevokingId(revokeState.keyId);
    const result = await revokeKeyAction(revokeState.keyId);
    setRevokingId(null);
    if (result.success) {
      // ACTIVE 필터 화면에서 revoke 한 행을 REVOKED 로 덮어쓰지 않고 제거 —
      // 필터 의미(활성만 보임)와 모순되는 유령 행을 남기지 않기 위함.
      onRevoked?.(revokeState.keyId);
      toast({
        type: 'success',
        message: t('revokeSuccess', { prefix: revokeState.keyPrefix }),
        auto_dismiss_ms: 4000,
      });
    } else {
      toast({
        type: 'error',
        message: result.error ?? t('revokeFailed'),
        auto_dismiss_ms: 5000,
      });
    }
  };

  if (keys.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 glass rounded-apple py-16 text-sm text-muted-foreground">
        <p>{t('noKeys')}</p>
        {/* 발급 동선 안내 — VK 는 gateway-cli 로만 발급되므로 빈 상태에서
            설정 페이지로 이어준다(리뷰: 빈 상태가 데드엔드) */}
        <Link href="/cli" className="text-xs text-primary hover:underline">
          {t('noKeysCliLink')}
        </Link>
      </div>
    );
  }

  return (
    <>
      <div className="glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('keyPrefix')}</Th>
              <Th>{t('userEmail')}</Th>
              <Th>{t('status')}</Th>
              <Th>{t('createdAt')}</Th>
              <Th>{t('expiresAt')}</Th>
              <Th numeric>{t('actions')}</Th>
            </Tr>
          </THead>
          <TBody>
            {keys.map((key) => (
              <Tr key={key.key_id}>
                <Td className="font-mono mono-id text-xs">{key.key_prefix}</Td>
                <Td className="text-foreground">
                  {key.user_email ?? <span className="text-muted-foreground">—</span>}
                </Td>
                <Td>
                  <Badge tone={STATUS_TONE[key.status] ?? 'neutral'}>
                    {t(`keyStatus.${key.status}` as 'keyStatus.ACTIVE' | 'keyStatus.EXPIRED' | 'keyStatus.REVOKED')}
                  </Badge>
                </Td>
                <Td className="text-muted-foreground">{formatDate(key.created_at, t('noExpiry'), tz)}</Td>
                <Td className="text-muted-foreground">{formatDate(key.expires_at, t('noExpiry'), tz)}</Td>
                <Td numeric>
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() =>
                        setRevokeState({
                          isOpen: true,
                          keyId: key.key_id,
                          keyPrefix: key.key_prefix,
                        })
                      }
                      disabled={
                        key.status !== KeyStatus.ACTIVE || revokingId === key.key_id
                      }
                      className="inline-flex items-center rounded-md border border-destructive/50 bg-background px-3 py-1.5 text-xs font-medium text-destructive hover:bg-destructive hover:text-destructive-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-40"
                    >
                      {revokingId === key.key_id ? t('revoking') : t('revoke')}
                    </button>
                  </div>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>

      {/* Revoke Confirm Dialog */}
      <ConfirmDialog
        isOpen={revokeState.isOpen}
        onClose={() => setRevokeState({ isOpen: false, keyId: '', keyPrefix: '' })}
        onConfirm={handleRevoke}
        title={t('revokeDialogTitle')}
        message={t('revokeDialogMessage', { prefix: revokeState.keyPrefix })}
        confirmLabel={t('revokeDialogConfirm')}
        isDestructive
      />
    </>
  );
}