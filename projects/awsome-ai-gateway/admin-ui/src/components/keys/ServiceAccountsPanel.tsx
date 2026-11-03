'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { createPortal } from 'react-dom';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDate } from '@/lib/utils/format';
import {
  issueServiceKeyAction,
  listIssueTargetsAction,
  markServiceAccountAction,
  repairServiceAccountAction,
  unmarkServiceAccountAction,
  type IssueTargetUser,
} from '@/lib/actions/service-accounts';
import { revokeKeyAction } from '@/lib/actions/keys';
import type { ServiceAccountItem } from '@/types/entities';

interface Props {
  accounts: ServiceAccountItem[];
  maxTtlDays: number;
}

export function ServiceAccountsPanel({ accounts, maxTtlDays }: Props) {
  const t = useTranslations('keys');
  const tz = useReportingTz();
  const { toast } = useToast();

  const [busy, setBusy] = useState<string | null>(null);
  const [unmarkTarget, setUnmarkTarget] = useState<ServiceAccountItem | null>(null);

  // ── 장기 키 발급 모달 ──
  const [issueOpen, setIssueOpen] = useState(false);
  const [issueUserId, setIssueUserId] = useState('');
  const [confirmText, setConfirmText] = useState('');
  const [ttlDays, setTtlDays] = useState<number>(maxTtlDays);
  const [issuedKey, setIssuedKey] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [allUsers, setAllUsers] = useState<IssueTargetUser[]>([]);

  const issueAccount = accounts.find((a) => a.user_id === issueUserId) ?? null;
  const selectedUser = allUsers.find((u) => u.id === issueUserId) ?? null;
  // 플래그 여부는 accounts prop(백엔드 서비스 계정 목록)으로 판별 — /admin/users
  // 응답엔 is_service_account 가 없다.
  const isFlagged = !!issueAccount;
  const activeAccounts = accounts.filter((a) => a.is_active);
  const confirmOk = confirmText.trim().toLowerCase() === 'confirm';
  const canIssue =
    !!selectedUser && ttlDays >= 1 && ttlDays <= maxTtlDays && confirmOk && busy === null;

  // 모달은 body 포털로 — glass 패널의 backdrop-filter 가 containing block 을
  // 만들어 fixed 가 뷰포트가 아니라 패널 기준으로 잡히는 문제(모달이 패널
  // 안쪽에 갇혀 위치가 이상해짐)를 피한다.
  const modalRoot = typeof document !== 'undefined' ? document.body : null;

  const run = async (id: string, fn: () => Promise<void>) => {
    setBusy(id);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };

  const openIssueModal = () => {
    setIssueUserId('');
    setConfirmText('');
    setTtlDays(maxTtlDays);
    setIssuedKey(null);
    setIssueOpen(true);
    // 모달을 여는 시점에 최신 목록을 가져온다 — 플래그되지 않은 유저도
    // 발급 대상이 될 수 있어(선택 시 자동 전환) 전체 활성 유저를 긁는다.
    listIssueTargetsAction().then((r) => {
      if (r.success) setAllUsers(r.data.items);
      else toast({ type: 'error', message: r.error ?? t('saIssueFailed'), auto_dismiss_ms: 5000 });
    });
  };

  const closeIssueModal = () => {
    setIssueOpen(false);
    setIssuedKey(null);
    setIssueUserId('');
    setConfirmText('');
    setCopied(false);
  };

  const handleCopy = (key: string) => {
    navigator.clipboard.writeText(key).then(
      () => {
        setCopied(true);
        toast({ type: 'success', message: t('saCopyToast'), auto_dismiss_ms: 2500 });
      },
      () => {
        toast({ type: 'error', message: t('saCopyFailed'), auto_dismiss_ms: 4000 });
      },
    );
  };

  const handleIssue = () =>
    run('issue', async () => {
      if (!selectedUser) return;
      // 미플래그 유저 선택 → 발급과 함께 서비스 계정으로 전환 (경고는 모달에 표시됨)
      if (!isFlagged) {
        const m = await markServiceAccountAction(selectedUser.email);
        if (!m.success) {
          toast({ type: 'error', message: m.error ?? t('saMarkFailed'), auto_dismiss_ms: 5000 });
          return;
        }
      }
      const r = await issueServiceKeyAction({
        userId: selectedUser.id,
        ttlDays: ttlDays || undefined,
      });
      if (r.success) {
        setIssuedKey(r.data.virtual_key ?? null);
      } else {
        toast({ type: 'error', message: r.error ?? t('saIssueFailed'), auto_dismiss_ms: 5000 });
      }
    });

  const doUnmark = async (a: ServiceAccountItem) => {
    // 활성 service 키가 있으면 먼저 폐기 — 백엔드는 활성 키가 남은 해제를 거부한다.
    if (a.active_key_id) {
      const rr = await revokeKeyAction(a.active_key_id);
      if (!rr.success) {
        toast({ type: 'error', message: rr.error ?? t('saUnmarkFailed'), auto_dismiss_ms: 5000 });
        return;
      }
    }
    const r = await unmarkServiceAccountAction(a.user_id);
    if (r.success) {
      toast({ type: 'success', message: t('saUnmarkDone'), auto_dismiss_ms: 4000 });
    } else {
      toast({ type: 'error', message: r.error ?? t('saUnmarkFailed'), auto_dismiss_ms: 5000 });
    }
  };

  const handleUnmark = () =>
    run('unmark', async () => {
      if (!unmarkTarget) return;
      await doUnmark(unmarkTarget);
      setUnmarkTarget(null);
    });

  const readinessBad = (a: ServiceAccountItem) =>
    !a.readiness.team_budget_ok ||
    !a.readiness.allowed_clients_clear ||
    a.readiness.usable_models === 0;

  return (
    <div className="glass rounded-apple p-4 mb-8">
      <div className="flex items-start justify-between gap-4 mb-4">
        <div>
          <h2 className="text-lg font-semibold mb-1">{t('saTitle')}</h2>
          <p className="text-xs text-muted-foreground">{t('saDescription')}</p>
        </div>
        <button
          onClick={openIssueModal}
          disabled={activeAccounts.length === 0 || busy !== null}
          className="shrink-0 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:opacity-90 disabled:opacity-40"
        >
          {t('saIssueButton')}
        </button>
      </div>

      {accounts.length > 0 && (
        <Table>
          <THead>
            <Tr>
              <Th>{t('saEmail')}</Th>
              <Th>{t('saTeam')}</Th>
              <Th>{t('saKey')}</Th>
              <Th>{t('expiresAt')}</Th>
              <Th>{t('saLastUsed')}</Th>
              <Th numeric>{t('actions')}</Th>
            </Tr>
          </THead>
          <TBody>
            {accounts.map((a) => (
              <Tr key={a.user_id}>
                <Td className="text-foreground">{a.email}</Td>
                <Td className="text-muted-foreground">{a.team_name ?? '—'}</Td>
                <Td>
                  {a.active_key_prefix ? (
                    <Badge tone="teal">{a.active_key_prefix}</Badge>
                  ) : (
                    <Badge tone="neutral">{t('saNoKey')}</Badge>
                  )}
                  {!a.is_active && <Badge tone="pink">{t('saInactive')}</Badge>}
                </Td>
                <Td className="text-muted-foreground">
                  {a.active_key_expires_at ? fmtDate(a.active_key_expires_at, tz) : '—'}
                </Td>
                <Td className="text-muted-foreground">
                  {a.last_request_at ? fmtDate(a.last_request_at, tz) : '—'}
                </Td>
                <Td numeric>
                  <div className="flex items-center justify-end gap-2">
                    <button
                      onClick={() =>
                        run(`repair-${a.user_id}`, async () => {
                          const r = await repairServiceAccountAction(a.user_id);
                          toast({
                            type: r.success ? 'success' : 'error',
                            message: r.success
                              ? t('saRepairDone', { count: r.data.repaired })
                              : (r.error ?? t('saRepairFailed')),
                            auto_dismiss_ms: 4000,
                          });
                        })
                      }
                      disabled={busy !== null || !a.active_key_prefix}
                      title={t('saRepairHint')}
                      className="rounded-md border border-border px-3 py-1.5 text-xs hover:bg-accent disabled:opacity-40"
                    >
                      {t('saRepair')}
                    </button>
                    <button
                      onClick={() => setUnmarkTarget(a)}
                      disabled={busy !== null}
                      className="rounded-md border border-destructive/50 px-3 py-1.5 text-xs text-destructive disabled:opacity-40"
                    >
                      {t('saUnmark')}
                    </button>
                  </div>
                </Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}

      {/* 해제 확인 다이얼로그 — 활성 키 있으면 폐기 경고 포함 */}
      {modalRoot &&
        unmarkTarget &&
        createPortal(
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div className="glass rounded-apple max-w-lg w-full p-6 bg-background">
              <h3 className="text-lg font-semibold mb-2">{t('saUnmarkTitle')}</h3>
              <p className="text-sm mb-2">
                {t('saUnmarkMessage', { email: unmarkTarget.email })}
              </p>
              {unmarkTarget.active_key_prefix && (
                <p className="text-sm text-destructive mb-2">
                  {t('saUnmarkRevokesKey', { prefix: unmarkTarget.active_key_prefix })}
                </p>
              )}
              <p className="text-xs text-muted-foreground mb-4">{t('saUnmarkHint')}</p>
              <div className="flex justify-end gap-2">
                <button
                  onClick={() => setUnmarkTarget(null)}
                  className="rounded-md border border-border px-3 py-2 text-sm"
                >
                  {t('saCancel')}
                </button>
                <button
                  onClick={handleUnmark}
                  disabled={busy === 'unmark'}
                  className="rounded-md bg-destructive px-3 py-2 text-sm text-destructive-foreground disabled:opacity-40"
                >
                  {t('saUnmarkConfirm')}
                </button>
              </div>
            </div>
          </div>,
          modalRoot,
        )}

      {/* ── 장기 키 발급 모달: 안내 → 계정 선택 → confirm 입력 → 발급 ── */}
      {modalRoot &&
        issueOpen &&
        !issuedKey &&
        createPortal(
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div className="glass rounded-apple max-w-lg w-full p-6 bg-background">
              <h3 className="text-lg font-semibold mb-3">{t('saIssueTitle')}</h3>
              <div className="text-sm text-muted-foreground space-y-1 mb-4">
                <p>{t('saGuide1')}</p>
                <p>{t('saGuide2')}</p>
              </div>

              <label className="block text-sm mb-4">
                {t('saSelectLabel')}
                <select
                  value={issueUserId}
                  onChange={(e) => setIssueUserId(e.target.value)}
                  className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
                >
                  <option value="">{t('saSelectPlaceholder')}</option>
                  {allUsers.map((u) => (
                    <option key={u.id} value={u.id}>
                      {u.email}
                      {u.team_name ? ` (${u.team_name})` : ''}
                      {accounts.some((a) => a.user_id === u.id) ? ' — ' + t('saAlreadyFlagged') : ''}
                    </option>
                  ))}
                </select>
              </label>

              {selectedUser && !isFlagged && (
                <p className="text-sm text-amber-500 mb-2">{t('saWillFlag')}</p>
              )}

              {issueAccount && (
                <>
                  {issueAccount.active_key_prefix && (
                    <p className="text-sm text-destructive mb-2">
                      {t('saReplaceWarning', { prefix: issueAccount.active_key_prefix })}
                    </p>
                  )}
                  <ul className="text-sm space-y-1 mb-4">
                    <li>
                      {issueAccount.readiness.team_budget_ok ? '✓' : '✗'} {t('saReadyBudget')}
                    </li>
                    <li>
                      {issueAccount.readiness.allowed_clients_clear ? '✓' : '✗'} {t('saReadyClients')}
                    </li>
                    <li>
                      {issueAccount.readiness.usable_models > 0 ? '✓' : '✗'}{' '}
                      {t('saReadyModels', { count: issueAccount.readiness.usable_models })}
                    </li>
                  </ul>
                  {readinessBad(issueAccount) && (
                    <p className="text-xs text-muted-foreground mb-3">{t('saReadyHint')}</p>
                  )}
                </>
              )}

              <label className="block text-sm mb-4">
                {t('saTtlLabel', { max: maxTtlDays })}
                <input
                  type="number"
                  min={1}
                  max={maxTtlDays}
                  value={ttlDays}
                  onChange={(e) => setTtlDays(Number(e.target.value))}
                  className="mt-1 w-32 rounded-md border border-border bg-background px-3 py-2 text-sm"
                />
              </label>

              <label className="block text-sm mb-1">
                {t('saTypeConfirm')}
                <input
                  type="text"
                  value={confirmText}
                  onChange={(e) => setConfirmText(e.target.value)}
                  placeholder="confirm"
                  autoComplete="off"
                  className="mt-1 w-full rounded-md border border-border bg-background px-3 py-2 text-sm font-mono"
                />
              </label>
              {confirmText.length > 0 && !confirmOk && (
                <p className="text-xs text-destructive mb-3">{t('saConfirmMismatch')}</p>
              )}
              {confirmText.length === 0 && <div className="mb-3" />}

              <div className="flex justify-end gap-2">
                <button
                  onClick={closeIssueModal}
                  className="rounded-md border border-border px-3 py-2 text-sm"
                >
                  {t('saCancel')}
                </button>
                <button
                  onClick={handleIssue}
                  disabled={!canIssue}
                  className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-40"
                >
                  {busy === 'issue' ? t('loading') : t('saIssueConfirm')}
                </button>
              </div>
            </div>
          </div>,
          modalRoot,
        )}

      {/* 발급 완료 — VK 1회 표시 + 복사 + 재확인 불가 경고 */}
      {modalRoot &&
        issueOpen &&
        issuedKey &&
        createPortal(
          <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
            <div className="glass rounded-apple max-w-lg w-full p-6 bg-background">
              <h3 className="text-lg font-semibold mb-2">{t('saKeyIssued')}</h3>
              <div className="flex items-center gap-2 mb-4">
                <code className="flex-1 break-all rounded border border-border bg-muted p-2 text-xs">
                  {issuedKey}
                </code>
                <button
                  onClick={() => handleCopy(issuedKey)}
                  className="min-w-16 rounded-md border border-border px-3 py-2 text-sm"
                >
                  {copied ? t('saCopied') : t('saCopy')}
                </button>
              </div>
              <p className="text-sm text-destructive mb-4">{t('saKeyOnce')}</p>
              <div className="flex justify-end">
                <button
                  onClick={closeIssueModal}
                  className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground"
                >
                  {t('saClose')}
                </button>
              </div>
            </div>
          </div>,
          modalRoot,
        )}
    </div>
  );
}
