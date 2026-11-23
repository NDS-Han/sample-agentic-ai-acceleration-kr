'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState } from 'react';
import { useTranslations } from 'next-intl';
import Link from 'next/link';
import { revokeKeyAction } from '@/lib/actions/keys';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { EmailText } from '@/components/common/UnverifiedEmailBadge';
import { Button } from '@/components/common/Button';
import { InfoTooltip } from '@/components/common/InfoTooltip';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { ReportingTzCaption } from '@/components/common/ReportingTzCaption';
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
  [KeyStatus.REVOKED]: 'neutral',
};

/**
 * 재발급(expire_and_create)은 기존 ACTIVE 키를 EXPIRED 로 바꾼다 — 만료일이 아직
 * 미래인 EXPIRED 는 '만료'가 아니라 '교체됨'이다. 백엔드 상태는 그대로 두고 UI 에서만
 * 구분한다. 파싱 불가/만료일 없음은 교체로 보지 않는다(fmtDate 와 같은 NaN 가드).
 */
function isSuperseded(key: Pick<VirtualKeyListItem, 'status' | 'expires_at'>, now: number): boolean {
  if (key.status !== KeyStatus.EXPIRED || !key.expires_at) return false;
  const exp = new Date(key.expires_at).getTime();
  return !Number.isNaN(exp) && exp > now;
}

function formatDate(iso: string | null, noExpiry: string, timeZone: string): string {
  if (!iso) return noExpiry;
  // 시각 없는 날짜(YYYY-MM-DD)라 리포팅 TZ 기준 — 표 아래 캡션으로 기준을 적는다.
  // 브라우저 TZ 로 바꾸면 보는 사람마다 날짜가 하루씩 어긋난다.
  return fmtDate(iso, timeZone);
}

export function KeysTable({ keys, onRevoked }: KeysTableProps) {
  const t = useTranslations('keys');
  const tz = useReportingTz();
  const { toast } = useToast();
  // 렌더마다 Date.now() 를 부르지 않도록 마운트 시점으로 고정한다.
  const [now] = useState(() => Date.now());

  const [revokeState, setRevokeState] = useState<{
    isOpen: boolean;
    keyId: string;
    keyPrefix: string;
  }>({ isOpen: false, keyId: '', keyPrefix: '' });

  // 단일 id 가 아니라 Set 으로 잠근다 — A 가 진행 중일 때 B 를 확인하면
  // 슬롯이 B 로 덮여 A 의 버튼이 풀리고, 같은 키를 중복 revoke 할 수 있었다.
  const [revokingIds, setRevokingIds] = useState<ReadonlySet<string>>(new Set());

  const handleRevoke = async () => {
    const keyId = revokeState.keyId;
    const keyPrefix = revokeState.keyPrefix;
    setRevokingIds((prev) => new Set(prev).add(keyId));
    try {
      const result = await revokeKeyAction(keyId);
      if (result.success) {
        // ACTIVE 필터 화면에서 revoke 한 행을 REVOKED 로 덮어쓰지 않고 제거 —
        // 필터 의미(활성만 보임)와 모순되는 유령 행을 남기지 않기 위함.
        // await 뒤의 revokeState 는 다음 확인 대상으로 바뀌어 있을 수 있으니 캡처값 사용.
        onRevoked?.(keyId);
        toast({
          type: 'success',
          message: t('revokeSuccess', { prefix: keyPrefix }),
          auto_dismiss_ms: 4000,
        });
      } else {
        toast({
          type: 'error',
          message: result.error ?? t('revokeFailed'),
          auto_dismiss_ms: 5000,
        });
      }
    } catch {
      // transport 거부 — 서버 반영 여부를 모르므로 행은 남기고 재시도를 안내한다.
      toast({ type: 'error', message: t('revokeFailed'), auto_dismiss_ms: 5000 });
    } finally {
      setRevokingIds((prev) => {
        if (!prev.has(keyId)) return prev;
        const next = new Set(prev);
        next.delete(keyId);
        return next;
      });
    }
  };

  if (keys.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 glass rounded-apple py-16 text-sm text-muted-foreground">
        <p>{t('noKeys')}</p>
        {/* 발급 동선 안내 — VK 는 gateway-cli 로만 발급되므로 빈 상태에서
            설정 페이지로 이어준다(리뷰: 빈 상태가 데드엔드) */}
        <Link href="/integration" className="text-xs text-primary hover:underline">
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
                  <EmailText email={key.user_email} fallback={<span className="text-muted-foreground">—</span>} />
                </Td>
                <Td>
                  <div className="flex items-center gap-1.5">
                    {isSuperseded(key, now) ? (
                      // UnverifiedEmailBadge(info) 와 같은 방식 — title + 키보드로도 열리는 InfoTooltip.
                      <span className="inline-flex items-center gap-1">
                        <Badge tone="neutral">
                          <span title={t('supersededHint')}>{t('keyStatus.SUPERSEDED')}</span>
                        </Badge>
                        <InfoTooltip label={t('keyStatus.SUPERSEDED')}>{t('supersededHint')}</InfoTooltip>
                      </span>
                    ) : (
                      <Badge tone={STATUS_TONE[key.status] ?? 'neutral'}>
                        {t(`keyStatus.${key.status}` as 'keyStatus.ACTIVE' | 'keyStatus.EXPIRED' | 'keyStatus.REVOKED')}
                      </Badge>
                    )}
                    {key.kind === 'service' && (
                      // 종류(상태 아님)는 neutral — 상태 teal 과 같은 색이면 '활성에이전트'로 읽힌다.
                      <Badge tone="neutral">{t('kindService')}</Badge>
                    )}
                  </div>
                </Td>
                <Td className="text-muted-foreground">{formatDate(key.created_at, t('noExpiry'), tz)}</Td>
                <Td className="text-muted-foreground">{formatDate(key.expires_at, t('noExpiry'), tz)}</Td>
                <Td numeric>
                  {/* 만료·폐기·교체된 키는 revoke 대상이 아니다 — 흐린 버튼 대신 '—' */}
                  {key.status === KeyStatus.ACTIVE ? (
                    <div className="flex items-center justify-end gap-2">
                      <Button
                        variant="dangerOutline"
                        size="sm"
                        onClick={() =>
                          setRevokeState({
                            isOpen: true,
                            keyId: key.key_id,
                            keyPrefix: key.key_prefix,
                          })
                        }
                        disabled={revokingIds.has(key.key_id)}
                      >
                        {revokingIds.has(key.key_id) ? t('revoking') : t('revoke')}
                      </Button>
                    </div>
                  ) : (
                    <span className="text-muted-foreground">—</span>
                  )}
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      </div>
      <ReportingTzCaption kind="dateOnly" className="mt-1 px-1" />

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