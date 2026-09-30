'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useMemo, useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { TeamBudgetAllocation } from '@/types/entities';
import {
  allocateTeamBudgetAction,
  deleteUserBudgetAction,
  equalSplitTeamBudgetAction,
  getTeamAllocationAction,
  setTeamDefaultCapAction,
} from '@/lib/actions/budgets';
import type { ConfirmationPayload } from '@/lib/actions/types';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { Table, THead, TBody, TFoot, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { ConfirmImpactBox } from './ConfirmImpactBox';

interface TeamAllocationViewProps {
  teamId: string;
  initialAllocation?: TeamBudgetAllocation | null;
  /** ADMIN 이면 임의 팀 편집 + D/균등분배 제어. TEAM_LEADER 는 멤버 cap 만. */
  isAdmin?: boolean;
  /** 본인 행 편집 잠금(D-13: 리더는 자기 예산을 못 바꾼다). */
  currentUserId?: string;
}

/** 입력 상태: 빈 문자열 = 개별 cap 없음(= D 또는 팀 한도 상속). */
type MemberInputs = Record<string, string>;

export function TeamAllocationView({
  teamId,
  initialAllocation,
  isAdmin = false,
  currentUserId,
}: TeamAllocationViewProps) {
  const t = useTranslations('budgets');
  const tCommon = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();

  const [allocation, setAllocation] = useState<TeamBudgetAllocation | null>(
    initialAllocation ?? null
  );
  const [loadFailed, setLoadFailed] = useState(false);
  const [inputs, setInputs] = useState<MemberInputs>({});
  const [search, setSearch] = useState('');
  const [unsetOnly, setUnsetOnly] = useState(false);
  // confirmation_required(409) → ConfirmImpactBox 표시 후 confirm=true 재시도.
  const [confirmation, setConfirmation] = useState<ConfirmationPayload | null>(null);
  const [pendingAction, setPendingAction] = useState<(() => void) | null>(null);
  // ADMIN 전용 D/균등분배 제어 상태.
  const [capInput, setCapInput] = useState('');
  const [clearIndividual, setClearIndividual] = useState(false);

  // entries → 입력값 초기화: 개별 cap 이 있는 멤버만 채우고, D/팀한도 멤버는
  // 빈 문자열(상속)로 둔다 — 상속자에게 A_u 를 덮어쓰지 않기 위함.
  const initInputs = (a: TeamBudgetAllocation): MemberInputs =>
    Object.fromEntries(
      a.entries
        .filter((e) => e.target_type === 'USER')
        .map((e) => [e.target_id, e.cap_source === 'individual' ? String(e.allocated_usd) : ''])
    );

  useEffect(() => {
    if (allocation) {
      setInputs(initInputs(allocation));
      setCapInput(
        allocation.default_user_cap_usd != null ? String(allocation.default_user_cap_usd) : ''
      );
      return;
    }
    // admin 임베딩 경로 — 서버에서 안 넘겨줬으면 클라이언트가 조회.
    startTransition(async () => {
      const r = await getTeamAllocationAction(teamId);
      if (r.success && r.data) {
        setAllocation(r.data);
        setInputs(initInputs(r.data));
        setCapInput(r.data.default_user_cap_usd != null ? String(r.data.default_user_cap_usd) : '');
      } else {
        setLoadFailed(true);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [teamId, allocation === null]);

  const refresh = async () => {
    const r = await getTeamAllocationAction(teamId);
    if (r.success && r.data) {
      setAllocation(r.data);
      setInputs(initInputs(r.data));
      setCapInput(r.data.default_user_cap_usd != null ? String(r.data.default_user_cap_usd) : '');
    }
  };

  const members = useMemo(() => allocation?.entries.filter((e) => e.target_type === 'USER') ?? [], [allocation]);

  const filteredMembers = useMemo(() => {
    const q = search.trim().toLowerCase();
    return members.filter((m) => {
      if (unsetOnly && m.cap_source === 'individual') return false;
      if (q && !m.target_name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [members, search, unsetOnly]);

  const totalBudget = allocation?.total_budget_usd ?? 0;
  const capD = allocation?.default_user_cap_usd ?? null;
  const overcommit = allocation?.overcommit_ratio ?? null;

  /** 저장 대상 수집 — 개별 cap 이 있는/새로 입력된 행만 보내고, 비워진 기존 개별
   *  cap 행은 별도로 삭제(D 상속으로 복귀)한다. */
  const collectChanges = () => {
    const toSet: { user_id: string; allocated_usd: number }[] = [];
    const toClear: string[] = [];
    for (const m of members) {
      const raw = (inputs[m.target_id] ?? '').trim();
      const hadIndividual = m.cap_source === 'individual';
      if (raw === '') {
        if (hadIndividual) toClear.push(m.target_id);
        continue;
      }
      const n = Number(raw);
      if (!Number.isFinite(n) || n < 0) return { error: t('invalidAmount', { name: m.target_name }) };
      if (!hadIndividual || n !== m.allocated_usd) {
        toSet.push({ user_id: m.target_id, allocated_usd: n });
      }
    }
    return { toSet, toClear };
  };

  const runSave = (confirm: boolean) => {
    startTransition(async () => {
      const changes = collectChanges();
      if ('error' in changes) {
        toast({ type: 'error', message: changes.error ?? '', auto_dismiss_ms: 5000 });
        return;
      }
      const { toSet, toClear } = changes;

      if (toSet.length > 0) {
        const r = await allocateTeamBudgetAction(
          teamId,
          toSet.map((i) => ({ target_id: i.user_id, target_type: 'USER' as const, allocated_usd: i.allocated_usd })),
          confirm
        );
        if (!r.success) {
          if (r.confirmation) {
            setConfirmation(r.confirmation);
            setPendingAction(() => () => runSave(true));
          } else {
            toast({ type: 'error', message: r.error, auto_dismiss_ms: 5000 });
          }
          return;
        }
      }
      for (const uid of toClear) {
        const r = await deleteUserBudgetAction(uid, confirm);
        if (!r.success) {
          if (r.confirmation) {
            setConfirmation(r.confirmation);
            setPendingAction(() => () => runSave(true));
          } else {
            toast({ type: 'error', message: r.error, auto_dismiss_ms: 5000 });
          }
          return;
        }
      }
      setConfirmation(null);
      toast({ type: 'success', message: t('allocationSaved'), auto_dismiss_ms: 3000 });
      await refresh();
    });
  };

  const runCapSave = (confirm: boolean) => {
    const trimmed = capInput.trim();
    const value = trimmed === '' ? null : Number(trimmed);
    if (value !== null && (!Number.isFinite(value) || value < 0)) {
      toast({ type: 'error', message: t('invalidDefaultCap'), auto_dismiss_ms: 5000 });
      return;
    }
    startTransition(async () => {
      const r = await setTeamDefaultCapAction(teamId, value, confirm);
      if (!r.success) {
        if (r.confirmation) {
          setConfirmation(r.confirmation);
          setPendingAction(() => () => runCapSave(true));
        } else {
          toast({ type: 'error', message: r.error, auto_dismiss_ms: 5000 });
        }
        return;
      }
      setConfirmation(null);
      toast({ type: 'success', message: t('defaultCapSaved'), auto_dismiss_ms: 3000 });
      await refresh();
    });
  };

  const runEqualSplit = (confirm: boolean) => {
    startTransition(async () => {
      const r = await equalSplitTeamBudgetAction(teamId, clearIndividual, confirm);
      if (!r.success) {
        if (r.confirmation) {
          setConfirmation(r.confirmation);
          setPendingAction(() => () => runEqualSplit(true));
        } else {
          toast({ type: 'error', message: r.error, auto_dismiss_ms: 5000 });
        }
        return;
      }
      setConfirmation(null);
      toast({ type: 'success', message: t('equalSplitDone'), auto_dismiss_ms: 3000 });
      await refresh();
    });
  };

  if (loadFailed) {
    return <p className="text-sm text-muted-foreground">{t('allocationLoadFailed')}</p>;
  }
  if (!allocation) {
    return <p className="text-sm text-muted-foreground py-4">{t('loadingText')}</p>;
  }

  const capSourceBadge = (src?: 'individual' | 'team_default' | null) => {
    const cls = 'inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium ';
    if (src === 'individual')
      return <span className={cls + 'bg-primary/10 text-primary'}>{t('capIndividual')}</span>;
    if (src === 'team_default')
      return <span className={cls + 'bg-secondary text-secondary-foreground'}>{t('capDefault')}</span>;
    return <span className={cls + 'bg-muted text-muted-foreground'}>{t('capNone')}</span>;
  };

  return (
    <div className="space-y-4">
      {/* 팀 헤더 — T, 사용량, D, 초과 약정률 */}
      <div className="rounded-lg border border-border bg-muted/30 px-4 py-3 space-y-2">
        <div className="flex items-center justify-between flex-wrap gap-2">
          <span className="text-sm font-medium">
            {t('teamTotalBudget', { team: allocation.team_name })}
          </span>
          <span className="text-lg font-bold">
            {totalBudget > 0 ? `$${totalBudget.toFixed(2)}` : t('notSet')}
          </span>
        </div>
        <div className="flex items-center gap-3 flex-wrap text-xs">
          <span className="text-muted-foreground">
            {t('defaultCapLabel')}:{' '}
            <span className="font-medium text-foreground">
              {capD != null ? `$${capD.toFixed(2)}` : t('notSet')}
            </span>
          </span>
          {overcommit != null && (
            <span
              className={
                overcommit > 1
                  ? 'inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium bg-destructive/10 text-destructive'
                  : 'inline-flex px-2 py-0.5 rounded-full text-[11px] font-medium bg-muted text-muted-foreground'
              }
              title={t('overcommitTooltip')}
            >
              {t('overcommitBadge', { pct: Math.round(overcommit * 100) })}
            </span>
          )}
        </div>

        {/* ADMIN 전용 — D 설정 + 균등분배 (§7 권한 매트릭스: ADMIN 만) */}
        {isAdmin && (
          <div className="flex items-end gap-3 flex-wrap pt-1 border-t border-border/60">
            <div>
              <label className="block text-xs text-muted-foreground mb-1">
                {t('defaultCapInputLabel')}
              </label>
              <div className="flex items-center gap-1">
                <span className="text-muted-foreground text-sm">$</span>
                <input
                  type="number"
                  min={0}
                  step={0.01}
                  value={capInput}
                  onChange={(e) => setCapInput(e.target.value)}
                  placeholder={t('notSetPlaceholder')}
                  className="w-28 rounded-md border border-input bg-background px-2 py-1 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                />
              </div>
            </div>
            <SpinnerButton
              type="button"
              onClick={() => runCapSave(false)}
              isLoading={isPending}
              className="px-3 py-1.5 text-xs"
            >
              {t('defaultCapSave')}
            </SpinnerButton>
            <div className="flex items-center gap-2">
              <SpinnerButton
                type="button"
                onClick={() => runEqualSplit(false)}
                isLoading={isPending}
                className="px-3 py-1.5 text-xs bg-secondary text-secondary-foreground hover:bg-secondary/80"
              >
                {t('equalSplit')}
              </SpinnerButton>
              <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
                <input
                  type="checkbox"
                  checked={clearIndividual}
                  onChange={(e) => setClearIndividual(e.target.checked)}
                  className="h-3.5 w-3.5 rounded border-border"
                />
                {t('equalSplitClear')}
              </label>
            </div>
          </div>
        )}
      </div>

      {confirmation && (
        <ConfirmImpactBox
          confirmation={confirmation}
          isPending={isPending}
          onConfirm={() => pendingAction?.()}
          onCancel={() => {
            setConfirmation(null);
            setPendingAction(null);
          }}
        />
      )}

      {/* 멤버 검색/필터 (D-22) */}
      <div className="flex items-center gap-3 flex-wrap">
        <input
          type="search"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder={t('searchMembers')}
          className="w-56 rounded-md border border-input bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        />
        <label className="flex items-center gap-1.5 text-xs text-muted-foreground cursor-pointer">
          <input
            type="checkbox"
            checked={unsetOnly}
            onChange={(e) => setUnsetOnly(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-border"
          />
          {t('filterUnsetOnly')}
        </label>
        <span className="text-xs text-muted-foreground">
          {t('memberCount', { shown: filteredMembers.length, total: members.length })}
        </span>
      </div>

      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('colMemberName')}</Th>
              <Th>{t('colCapSource')}</Th>
              <Th numeric>{t('colEffectiveCap')}</Th>
              <Th>{t('colAllocated')}</Th>
              <Th numeric>{t('colUsage')}</Th>
            </Tr>
          </THead>
          <TBody>
            {filteredMembers.length === 0 ? (
              <TEmpty colSpan={5}>{t('noMembers')}</TEmpty>
            ) : (
              filteredMembers.map((m) => {
                const isSelf = currentUserId != null && m.target_id === currentUserId;
                const lockRow = isSelf && !isAdmin;
                return (
                  <Tr key={m.target_id}>
                    <Td emphasis>
                      {m.target_name}
                      {lockRow && (
                        <span className="ml-1 text-[11px] text-muted-foreground">
                          {t('selfRowLocked')}
                        </span>
                      )}
                    </Td>
                    <Td>{capSourceBadge(m.cap_source)}</Td>
                    <Td numeric className="text-muted-foreground">
                      {m.effective_cap_usd != null ? `$${m.effective_cap_usd.toFixed(2)}` : '-'}
                    </Td>
                    <Td>
                      <div className="flex items-center gap-1">
                        <span className="text-muted-foreground">$</span>
                        <input
                          type="number"
                          min={0}
                          step={0.01}
                          value={inputs[m.target_id] ?? ''}
                          disabled={lockRow || isPending}
                          onChange={(e) =>
                            setInputs((prev) => ({ ...prev, [m.target_id]: e.target.value }))
                          }
                          placeholder={m.cap_source === 'team_default' ? t('inheritD') : t('inheritTeam')}
                          className="w-28 rounded-md border border-input bg-background px-2 py-1 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
                        />
                      </div>
                    </Td>
                    <Td numeric className="text-muted-foreground">
                      ${m.used_usd.toFixed(2)}
                    </Td>
                  </Tr>
                );
              })
            )}
          </TBody>
          <TFoot>
            <Tr>
              <Td emphasis colSpan={4}>{t('sumAllocated')}</Td>
              <Td numeric className="text-muted-foreground">
                ${(allocation.sum_allocated_usd ?? 0).toFixed(2)}
              </Td>
            </Tr>
          </TFoot>
        </Table>
      </div>

      <div className="flex items-center justify-between gap-3 flex-wrap">
        <p className="text-xs text-muted-foreground">{t('allocationHint')}</p>
        <SpinnerButton onClick={() => runSave(false)} isLoading={isPending} type="button">
          {tCommon('save')}
        </SpinnerButton>
      </div>
    </div>
  );
}
