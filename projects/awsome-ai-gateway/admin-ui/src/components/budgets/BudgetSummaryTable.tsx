'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Fragment, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import { ChevronRight, ChevronDown, Users } from 'lucide-react';
import type { BudgetSummaryItem, ModelListItem } from '@/types/entities';
import { AlertLevel, BudgetScope } from '@/types/enums';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { AppDialog } from '@/components/common/AppDialog';
import { SetBudgetDialog } from './SetBudgetDialog';
import { AutoDowngradeConfig } from './AutoDowngradeConfig';
import { TeamAllocationView } from './TeamAllocationView';
import { AlertBadge, TypeBadge, UsageBar } from './budgetVisuals';
import { fmtUsd } from '@/lib/utils/format';

interface BudgetSummaryTableProps {
  items: BudgetSummaryItem[];
  isAdmin: boolean;
  models: ModelListItem[];
  currentUserId?: string;
  /** /users 패널 딥링크 — ?team= 행 펼침·스크롤, ?user= 예산 다이얼로그 오픈. */
  focusTeam?: string;
  focusUser?: string;
}

type DialogTarget = {
  id: string;
  name: string;
  type: (typeof BudgetScope)[keyof typeof BudgetScope];
  currentLimit: number;
  currentUsed?: number;
  parentLimit?: number;
  /** USER 행: 소속 팀의 기본 cap D — 입력 참고값 표시용 (D-7). */
  teamDefaultCap?: number | null;
  /** USER 행: 현재 cap 출처 — 다이얼로그의 상속 상태 안내용. */
  capSource?: 'individual' | 'team_default' | null;
  /** TEAM 행: 현재 D — 다이얼로그의 D 입력 초기값. */
  currentDefaultCap?: number | null;
};

const UNASSIGNED_KEY = '__unassigned__';

export function BudgetSummaryTable({ items, isAdmin, models, currentUserId, focusTeam, focusUser }: BudgetSummaryTableProps) {
  const t = useTranslations('budgets');
  const [selectedItem, setSelectedItem] = useState<DialogTarget | null>(null);
  const [isDialogOpen, setIsDialogOpen] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [showInactive, setShowInactive] = useState(true);
  // D-21: admin 이 펼친 팀 행에서 "팀원 일괄 편집" 모달을 여는 대상.
  const [memberEditTeam, setMemberEditTeam] = useState<{ id: string; name: string } | null>(null);

  const alertLabels: Record<string, string> = {
    [AlertLevel.NORMAL]: t('alertLevels.NORMAL'),
    [AlertLevel.WARNING]: t('alertLevels.WARNING'),
    [AlertLevel.CRITICAL]: t('alertLevels.CRITICAL'),
  };
  const typeLabels: Record<string, string> = {
    [BudgetScope.TEAM]: t('scope.TEAM'),
    [BudgetScope.USER]: t('scope.USER'),
  };

  const hasInactive = items.some(i => i.is_active === false);
  const filteredItems = showInactive ? items : items.filter(i => i.is_active !== false);

  const { teamRows, usersByTeam, unassignedUsers } = useMemo(() => {
    const teams = filteredItems.filter((i) => i.target_type === BudgetScope.TEAM);
    const users = filteredItems.filter((i) => i.target_type === BudgetScope.USER);
    const grouped: Record<string, BudgetSummaryItem[]> = {};
    const orphans: BudgetSummaryItem[] = [];
    for (const u of users) {
      if (u.team_id) {
        (grouped[u.team_id] ??= []).push(u);
      } else {
        orphans.push(u);
      }
    }
    return { teamRows: teams, usersByTeam: grouped, unassignedUsers: orphans };
  }, [filteredItems]);

  // 딥링크 포커스 — 1회만 적용한다(Strict Mode 이중 실행 가드는 effect
  // 최상단에서 세워야 두 번째 호출이 통과하지 못한다). ref 가드가 재실행을
  // 막으므로 deps 는 정직하게 채운다 — 이후 수동 토글/필터 변경은 덮지 않는다.
  const focusAppliedRef = useRef(false);
  useEffect(() => {
    if (focusAppliedRef.current) return;
    focusAppliedRef.current = true;
    const team = focusTeam
      ? items.find((i) => i.target_type === BudgetScope.TEAM && i.target_id === focusTeam)
      : undefined;
    const user = focusUser
      ? items.find((i) => i.target_type === BudgetScope.USER && i.target_id === focusUser)
      : undefined;
    // 대상이 비활성이면 includeInactive 필터에 걸릴 수 있으니 강제로 켠다.
    if ((team ?? user)?.is_active === false) setShowInactive(true);
    if (team) {
      setExpanded((prev) => ({ ...prev, [team.target_id]: true }));
      document
        .getElementById(`budget-row-${team.target_id}`)
        ?.scrollIntoView({ block: 'center' });
    }
    if (user) {
      const groupKey = user.team_id ?? UNASSIGNED_KEY;
      setExpanded((prev) => ({ ...prev, [groupKey]: true }));
      // 유저 행은 펼침 후에 렌더되므로 항상 렌더된 팀/그룹 행으로 스크롤한다.
      document
        .getElementById(`budget-row-${groupKey}`)
        ?.scrollIntoView({ block: 'center' });
      // handleOpenDialog 와 같은 구성 — effect 안에서 함수 참조를 피하기 위해 인라인.
      const parentTeam = user.team_id
        ? items.find((i) => i.target_type === BudgetScope.TEAM && i.target_id === user.team_id)
        : undefined;
      setSelectedItem({
        id: user.target_id,
        name: user.target_name,
        type: user.target_type,
        currentLimit: user.limit ?? 0,
        currentUsed: user.used,
        teamDefaultCap: parentTeam?.default_user_cap_usd ?? null,
        capSource: user.cap_source ?? null,
        currentDefaultCap: null,
      });
      setIsDialogOpen(true);
    }
  }, [items, focusTeam, focusUser]);

  const handleOpenDialog = (item: BudgetSummaryItem) => {
    // USER 행: 소속 팀의 D 를 찾아 다이얼로그 참고값으로 넘긴다 (D-7).
    const parentTeam =
      item.target_type === BudgetScope.USER && item.team_id
        ? teamRows.find((tm) => tm.target_id === item.team_id)
        : undefined;
    setSelectedItem({
      id: item.target_id,
      name: item.target_name,
      type: item.target_type,
      currentLimit: item.limit ?? 0,
      currentUsed: item.used,
      // parentLimit 을 넘기지 않는다 — CAP 모델은 A_u > T(초과 약정)를 허용하므로
      // 팀 예산이 개별 cap 의 상한이 아니다.
      teamDefaultCap: parentTeam?.default_user_cap_usd ?? null,
      capSource: item.cap_source ?? null,
      currentDefaultCap: item.target_type === BudgetScope.TEAM ? item.default_user_cap_usd ?? null : null,
    });
    setIsDialogOpen(true);
  };

  const handleCloseDialog = () => {
    setIsDialogOpen(false);
    setSelectedItem(null);
  };

  const toggle = (key: string) => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }));
  };

  const colCount = isAdmin ? 8 : 7;
  const isEmpty = teamRows.length === 0 && unassignedUsers.length === 0;

  const renderUserRow = (user: BudgetSummaryItem) => (
    <Tr key={user.target_id} className="bg-muted/10">
      <Td emphasis>
        <div className="flex items-center gap-2 pl-10">
          <span className="text-muted-foreground" aria-hidden="true">
            └
          </span>
          {user.target_name}
        </div>
      </Td>
      <Td>
        <TypeBadge type={user.target_type} labels={typeLabels} />
      </Td>
      <Td numeric>
        {user.limit != null ? (
          fmtUsd(user.limit)
        ) : user.cap_source === 'team_default' ? (
          <span className="text-muted-foreground italic">{t('defaultCapApplied')}</span>
        ) : (
          <span className="text-muted-foreground italic">{t('teamBudgetApplied')}</span>
        )}
      </Td>
      <Td numeric>{fmtUsd(user.used)}</Td>
      <Td numeric>
        {user.remaining != null ? fmtUsd(user.remaining) : <span className="text-muted-foreground italic">-</span>}
      </Td>
      <Td>
        <div className="flex items-center gap-2">
          {user.usage_pct != null ? (
            <>
              <UsageBar pct={user.usage_pct} level={user.alert_level} />
              <span className="w-12 text-right text-[11px] tabular-nums text-muted-foreground whitespace-nowrap">
                {user.usage_pct.toFixed(1)}%
              </span>
            </>
          ) : (
            <span className="text-xs text-muted-foreground italic">-</span>
          )}
        </div>
      </Td>
      <Td>
        <AlertBadge level={user.alert_level} labels={alertLabels} />
      </Td>
      {isAdmin && (
        <Td>
          <button
            onClick={() => handleOpenDialog(user)}
            className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {t('setBudget')}
          </button>
        </Td>
      )}
    </Tr>
  );

  return (
    <>
      {hasInactive && (
        <div className="flex items-center gap-2 mb-3">
          <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={e => setShowInactive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-border"
            />
            {t('includeInactive')}
          </label>
        </div>
      )}
      <div className="w-full glass rounded-apple overflow-hidden">
        <Table>
          <THead>
            <Tr>
              <Th>{t('targetName')}</Th>
              <Th>{t('type')}</Th>
              <Th numeric>{t('maxBudget')}</Th>
              <Th numeric>{t('used')}</Th>
              <Th numeric>{t('remainingBudget')}</Th>
              <Th className="min-w-[120px]">{t('usageRate')}</Th>
              <Th>{t('status')}</Th>
              {isAdmin && <Th>{t('actions')}</Th>}
            </Tr>
          </THead>
          <TBody>
            {isEmpty ? (
              <TEmpty colSpan={colCount}>{t('noData')}</TEmpty>
            ) : (
              <>
                {teamRows.map((team) => {
                  const members = usersByTeam[team.target_id] ?? [];
                  const isOpen = expanded[team.target_id] ?? false;
                  const hasMembers = members.length > 0;
                  return (
                    <Fragment key={team.target_id}>
                      <Tr id={`budget-row-${team.target_id}`}>
                        <Td emphasis>
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              onClick={() => toggle(team.target_id)}
                              aria-expanded={isOpen}
                              aria-label={
                                isOpen
                                  ? t('collapse', { name: team.target_name })
                                  : t('expand', { name: team.target_name })
                              }
                              className="flex h-5 w-5 items-center justify-center rounded hover:bg-muted text-muted-foreground"
                            >
                              {isOpen ? (
                                <ChevronDown size={14} />
                              ) : (
                                <ChevronRight size={14} />
                              )}
                            </button>
                            <span>{team.target_name}</span>
                            {hasMembers && (
                              <span className="text-xs text-muted-foreground">
                                ({members.length})
                              </span>
                            )}
                            {/* 다운그레이드 최신 배치 규칙 수 — 접힌 상태에서도
                                설정 유무가 보인다. 꺼진 배치는 neutral 로 구분. */}
                            {(team.downgrade_rule_count ?? 0) > 0 && (
                              <span
                                className={`badge whitespace-nowrap ${
                                  team.downgrade_enabled ? 'badge-sky' : 'badge-neutral'
                                }`}
                                title={
                                  team.downgrade_enabled
                                    ? undefined
                                    : t('downgradeBadgeOff')
                                }
                              >
                                {t('downgradeBadge', { count: team.downgrade_rule_count! })}
                              </span>
                            )}
                          </div>
                        </Td>
                        <Td>
                          <TypeBadge type={team.target_type} labels={typeLabels} />
                        </Td>
                        <Td numeric>
                          {team.limit != null ? fmtUsd(team.limit) : <span className="text-muted-foreground italic">{t('notSet')}</span>}
                          {team.default_user_cap_usd != null && (
                            <span className="block text-[11px] text-muted-foreground tabular-nums">
                              {t('defaultCapShort', { value: fmtUsd(team.default_user_cap_usd) })}
                            </span>
                          )}
                        </Td>
                        <Td numeric>{fmtUsd(team.used)}</Td>
                        <Td numeric>
                          {team.remaining != null ? fmtUsd(team.remaining) : <span className="text-muted-foreground italic">-</span>}
                        </Td>
                        <Td>
                          <div className="flex items-center gap-2">
                            {team.usage_pct != null ? (
                              <>
                                <UsageBar pct={team.usage_pct} level={team.alert_level} />
                                <span className="w-12 text-right text-[11px] tabular-nums text-muted-foreground whitespace-nowrap">
                                  {team.usage_pct.toFixed(1)}%
                                </span>
                              </>
                            ) : (
                              <span className="text-xs text-muted-foreground italic">-</span>
                            )}
                          </div>
                        </Td>
                        <Td>
                          <AlertBadge level={team.alert_level} labels={alertLabels} />
                        </Td>
                        {isAdmin && (
                          <Td>
                            <button
                              onClick={() => handleOpenDialog(team)}
                              className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                            >
                              {t('setBudget')}
                            </button>
                          </Td>
                        )}
                      </Tr>
                      {isOpen && members.map(renderUserRow)}
                      {isOpen && isAdmin && (
                        <Tr className="bg-muted/10">
                          <Td colSpan={colCount}>
                            <div className="pl-10 py-1">
                              <button
                                type="button"
                                onClick={() =>
                                  setMemberEditTeam({ id: team.target_id, name: team.target_name })
                                }
                                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                              >
                                <Users size={13} aria-hidden="true" />
                                {t('memberBulkEdit')}
                              </button>
                            </div>
                          </Td>
                        </Tr>
                      )}
                      {isOpen && (
                        <Tr className="bg-muted/10">
                          <Td colSpan={colCount}>
                            <div className="pl-10 py-2">
                              <AutoDowngradeConfig
                                scopeType="TEAM"
                                scopeId={team.target_id}
                                scopeName={team.target_name}
                                models={models}
                                currentUsagePct={team.usage_pct}
                              />
                            </div>
                          </Td>
                        </Tr>
                      )}
                    </Fragment>
                  );
                })}

                {unassignedUsers.length > 0 && (() => {
                  const isOpen = expanded[UNASSIGNED_KEY] ?? false;
                  return (
                    <Fragment key={UNASSIGNED_KEY}>
                      <Tr id={`budget-row-${UNASSIGNED_KEY}`}>
                        <Td emphasis colSpan={colCount}>
                          <div className="flex items-center gap-2">
                            <button
                              type="button"
                              onClick={() => toggle(UNASSIGNED_KEY)}
                              aria-expanded={isOpen}
                              aria-label={isOpen ? t('collapseUnassigned') : t('expandUnassigned')}
                              className="flex h-5 w-5 items-center justify-center rounded hover:bg-muted text-muted-foreground"
                            >
                              {isOpen ? (
                                <ChevronDown size={14} />
                              ) : (
                                <ChevronRight size={14} />
                              )}
                            </button>
                            <span className="text-muted-foreground">{t('unassigned')}</span>
                            <span className="text-xs text-muted-foreground">
                              ({unassignedUsers.length})
                            </span>
                          </div>
                        </Td>
                      </Tr>
                      {isOpen && unassignedUsers.map(renderUserRow)}
                    </Fragment>
                  );
                })()}
              </>
            )}
          </TBody>
        </Table>
      </div>

      <SetBudgetDialog
        key={selectedItem?.id ?? 'none'}
        isOpen={isDialogOpen}
        onClose={handleCloseDialog}
        target={selectedItem}
      />

      {/* D-21: admin 팀원 일괄 편집 모달 — allocation 은 뷰 내부에서 lazy-load. */}
      <AppDialog
        isOpen={memberEditTeam !== null}
        onClose={() => setMemberEditTeam(null)}
        title={memberEditTeam ? t('memberBulkEditTitle', { team: memberEditTeam.name }) : ''}
        contentClassName="max-w-4xl"
      >
        {memberEditTeam && (
          <TeamAllocationView
            key={memberEditTeam.id}
            teamId={memberEditTeam.id}
            isAdmin
            currentUserId={currentUserId}
          />
        )}
      </AppDialog>
    </>
  );
}