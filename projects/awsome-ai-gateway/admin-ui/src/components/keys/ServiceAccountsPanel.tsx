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
  const [issueTarget, setIssueTarget] = useState<ServiceAccountItem | null>(null);
  const [unmarkTarget, setUnmarkTarget] = useState<ServiceAccountItem | null>(null);
  const [ttlDays, setTtlDays] = useState<number>(maxTtlDays);
  const [issuedKey, setIssuedKey] = useState<string | null>(null);
  const [storedConfirm, setStoredConfirm] = useState(false);
  const searchRef = useRef<HTMLDivElement>(null);

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
      if (!issueTarget) return;
      const r = await issueServiceKeyAction({
        userId: issueTarget.user_id,
        ttlDays: ttlDays || undefined,
      });
      if (r.success) {
        setIssuedKey(r.data.virtual_key ?? null);
        setStoredConfirm(false);
      } else {
        toast({ type: 'error', message: r.error ?? t('saIssueFailed'), auto_dismiss_ms: 5000 });
        setIssueTarget(null);
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
      <h2 className="text-lg font-semibold mb-1">{t('saTitle')}</h2>
      <p className="text-xs text-muted-foreground mb-4">{t('saDescription')}</p>

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
                      onClick={() => {
                        setIssueTarget(a);
                        setTtlDays(maxTtlDays);
                        setIssuedKey(null);
                      }}
                      disabled={busy !== null || !a.is_active}
                      className="rounded-md border border-primary/50 px-3 py-1.5 text-xs text-primary hover:bg-primary hover:text-primary-foreground disabled:opacity-40"
                    >
                      {a.active_key_prefix ? t('saReissue') : t('saIssue')}
                    </button>
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

      {/* Issue dialog */}
      {issueTarget && !issuedKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="glass rounded-apple max-w-lg w-full p-6 bg-background">
            <h3 className="text-lg font-semibold mb-2">
              {t('saIssueTitle', { email: issueTarget.email })}
            </h3>
            {issueTarget.active_key_prefix && (
              <p className="text-sm text-destructive mb-2">
                {t('saReplaceWarning', { prefix: issueTarget.active_key_prefix })}
              </p>
            )}
            <ul className="text-sm space-y-1 mb-4">
              <li>
                {issueTarget.readiness.team_budget_ok ? '✓' : '✗'}{' '}
                {t('saReadyBudget')}
              </li>
              <li>
                {issueTarget.readiness.allowed_clients_clear ? '✓' : '✗'}{' '}
                {t('saReadyClients')}
              </li>
              <li>
                {issueTarget.readiness.usable_models > 0 ? '✓' : '✗'}{' '}
                {t('saReadyModels', { count: issueTarget.readiness.usable_models })}
              </li>
            </ul>
            {readinessBad(issueTarget) && (
              <p className="text-xs text-muted-foreground mb-3">
                {t('saReadyHint')}
              </p>
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
            <div className="flex justify-end gap-2">
              <button
                onClick={() => setIssueTarget(null)}
                className="rounded-md border border-border px-3 py-2 text-sm"
              >
                {t('saCancel')}
              </button>
              <button
                onClick={handleIssue}
                disabled={busy === 'issue' || ttlDays < 1 || ttlDays > maxTtlDays}
                className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-40"
              >
                {t('saIssueConfirm')}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Show-once key dialog */}
      {issuedKey && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50">
          <div className="glass rounded-apple max-w-lg w-full p-6 bg-background">
            <h3 className="text-lg font-semibold mb-2">{t('saKeyIssued')}</h3>
            <p className="text-xs text-muted-foreground mb-3">{t('saKeyOnce')}</p>
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
            <label className="flex items-center gap-2 text-sm mb-4">
              <input
                type="checkbox"
                checked={storedConfirm}
                onChange={(e) => setStoredConfirm(e.target.checked)}
              />
              {t('saStoredConfirm')}
            </label>
            <div className="flex justify-end">
              <button
                onClick={() => {
                  setIssuedKey(null);
                  setIssueTarget(null);
                }}
                disabled={!storedConfirm}
                className="rounded-md bg-primary px-3 py-2 text-sm text-primary-foreground disabled:opacity-40"
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
