'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Badge } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDate } from '@/lib/utils/format';
import {
  issueServiceKeyAction,
  markServiceAccountAction,
  repairServiceAccountAction,
  unmarkServiceAccountAction,
} from '@/lib/actions/service-accounts';
import { searchUsersAction } from '@/lib/actions/users';
import { revokeKeyAction } from '@/lib/actions/keys';
import type { ServiceAccountItem, UserSearchItem } from '@/types/entities';

interface Props {
  accounts: ServiceAccountItem[];
  maxTtlDays: number;
}

export function ServiceAccountsPanel({ accounts, maxTtlDays }: Props) {
  const t = useTranslations('keys');
  const tz = useReportingTz();
  const { toast } = useToast();

  // ── 사용자 검색 → 서비스 계정 등록 ──
  const [query, setQuery] = useState('');
  const [results, setResults] = useState<UserSearchItem[]>([]);
  const [searching, setSearching] = useState(false);
  const [pendingUser, setPendingUser] = useState<UserSearchItem | null>(null);

  const [busy, setBusy] = useState<string | null>(null);
  const [unmarkTarget, setUnmarkTarget] = useState<ServiceAccountItem | null>(null);

  // ── 장기 키 발급 모달 ──
  const [issueOpen, setIssueOpen] = useState(false);
  const [issueUserId, setIssueUserId] = useState('');
  const [confirmText, setConfirmText] = useState('');
  const [ttlDays, setTtlDays] = useState<number>(maxTtlDays);
  const [issuedKey, setIssuedKey] = useState<string | null>(null);

  const searchRef = useRef<HTMLDivElement>(null);

  const issueAccount = accounts.find((a) => a.user_id === issueUserId) ?? null;
  const activeAccounts = accounts.filter((a) => a.is_active);
  const confirmOk = confirmText.trim().toLowerCase() === 'confirm';
  const canIssue =
    !!issueAccount && ttlDays >= 1 && ttlDays <= maxTtlDays && confirmOk && busy === null;

  // 입력 debounce — 이미 서비스 계정인 유저는 후보에서 제외.
  useEffect(() => {
    if (query.trim().length < 2) {
      setResults([]);
      return;
    }
    const timer = setTimeout(async () => {
      setSearching(true);
      const r = await searchUsersAction(query);
      if (r.success) {
        const known = new Set(accounts.map((a) => a.user_id));
        setResults(r.data.items.filter((u) => !known.has(u.id)));
      }
      setSearching(false);
    }, 300);
    return () => clearTimeout(timer);
  }, [query, accounts]);

  // 바깥 클릭 시 드롭다운 닫기
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (searchRef.current && !searchRef.current.contains(e.target as Node)) {
        setResults([]);
      }
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);

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
  };

  const closeIssueModal = () => {
    setIssueOpen(false);
    setIssuedKey(null);
    setIssueUserId('');
    setConfirmText('');
  };

  const handleMark = () =>
    run('mark', async () => {
      if (!pendingUser) return;
      const r = await markServiceAccountAction(pendingUser.email);
      if (r.success) {
        setPendingUser(null);
        setQuery('');
        toast({ type: 'success', message: t('saMarkSuccess'), auto_dismiss_ms: 4000 });
      } else {
        toast({ type: 'error', message: r.error ?? t('saMarkFailed'), auto_dismiss_ms: 5000 });
      }
    });

  const handleIssue = () =>
    run('issue', async () => {
      if (!issueAccount) return;
      const r = await issueServiceKeyAction({
        userId: issueAccount.user_id,
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

      {/* 사용자 검색 → 서비스 계정 등록 */}
      <div ref={searchRef} className="relative mb-4 max-w-md">
        <input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t('saSearchPlaceholder')}
          className="w-full rounded-md border border-border bg-background px-3 py-2 text-sm"
        />
        {searching && (
          <p className="absolute mt-1 text-xs text-muted-foreground">{t('loading')}</p>
        )}
        {results.length > 0 && (
          <ul className="absolute z-10 mt-1 w-full rounded-md border border-border bg-background shadow-lg">
            {results.map((u) => (
              <li key={u.id}>
                <button
                  type="button"
                  onClick={() => {
                    setPendingUser(u);
                    setResults([]);
                  }}
                  className="flex w-full flex-col px-3 py-2 text-left hover:bg-accent"
                >
                  <span className="text-sm text-foreground">{u.email}</span>
                  <span className="text-xs text-muted-foreground">
                    {u.display_name}
                    {u.team_name ? ` · ${u.team_name}` : ''}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* 선택된 사용자 → 등록 확인 */}
      {pendingUser && (
        <div className="mb-4 flex items-center gap-3 rounded-md border border-border bg-muted/40 px-3 py-2">
          <div className="flex-1">
            <p className="text-sm font-medium">{pendingUser.email}</p>
            <p className="text-xs text-muted-foreground">{t('saMarkConfirmHint')}</p>
          </div>
          <button
            onClick={handleMark}
            disabled={busy === 'mark'}
            className="rounded-md bg-primary px-3 py-1.5 text-xs text-primary-foreground disabled:opacity-40"
          >
            {t('saMark')}
          </button>
          <button
            onClick={() => setPendingUser(null)}
            className="rounded-md border border-border px-3 py-1.5 text-xs"
          >
            {t('saCancel')}
          </button>
        </div>
      )}

      {accounts.length > 0 && (
        <Table>
          <THead>
            <Tr>
              <Th>{t('saEmail')}</Th>
              <Th>{t('saTeam')}</Th>
              <Th>{t('status')}</Th>
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
      {unmarkTarget && (
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
        </div>
      )}

      {/* ── 장기 키 발급 모달: 안내 → 계정 선택 → confirm 입력 → 발급 ── */}
      {issueOpen && !issuedKey && (
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
                {activeAccounts.map((a) => (
                  <option key={a.user_id} value={a.user_id}>
                    {a.email}
                    {a.team_name ? ` (${a.team_name})` : ''}
                  </option>
                ))}
              </select>
            </label>

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

            <label className="block text-sm mb-4">
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
        </div>
      )}

      {/* 발급 완료 — VK 1회 표시 + 복사 + 재확인 불가 경고 */}
      {issueOpen && issuedKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="glass rounded-apple max-w-lg w-full p-6 bg-background">
            <h3 className="text-lg font-semibold mb-2">{t('saKeyIssued')}</h3>
            <div className="flex items-center gap-2 mb-4">
              <code className="flex-1 break-all rounded border border-border bg-muted p-2 text-xs">
                {issuedKey}
              </code>
              <button
                onClick={() => navigator.clipboard.writeText(issuedKey)}
                className="rounded-md border border-border px-3 py-2 text-sm"
              >
                {t('saCopy')}
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
        </div>
      )}
    </div>
  );
}
