'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 팀 상세 — 머리(전체 팀 이름·부서·구성원 수·팀장, 팀장 변경·Analytics), 사용량 카드 5장,
 * 적용 중인 정책 6줄(값 · 출처 · 변경), 구성원 표(행 → 사용자 상세), 위험 작업(강제 재인증).
 * 정책 편집은 항목별 대화상자 — 통합 Apply 바는 없다.
 */

import { useMemo, useState, useTransition } from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ArrowUpRight } from 'lucide-react';

import { AppDialog } from '@/components/common/AppDialog';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { Table, TBody, Td, TEmpty, THead, Th, Tr } from '@/components/common/Table';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { UnverifiedEmailBadge } from '@/components/common/UnverifiedEmailBadge';
import { usePointInTime } from '@/components/common/usePointInTime';
import { forceReauthTeamAction } from '@/lib/actions/users';
import { fmtTokensCount, fmtUsd } from '@/lib/utils/format';
import { userLabel, userNameParts } from '@/lib/userLabel';
import type { OrgTreeNode } from '@/types/entities';

import { AllowedModelsDialog } from './AllowedModelsDialog';
import { AppAccessDialog } from './AppAccessDialog';
import { LeaderDialog } from './LeaderDialog';
import { KpiCard, KpiSkeleton, PolicyRow, Section } from './parts';
import {
  appsSummary,
  budgetSummary,
  downgradeSummary,
  modelsSummary,
  rateSummary,
  webSearchSummary,
} from './policyRows';
import { RateLimitDialog } from './RateLimitDialog';
import { RateLimitUsageCard } from './RateLimitUsageCard';
import { EMPTY_CELL, useActivity, useCatalog, useCurrentPeriod, usePolicy, useTeamUsage } from './useDetailData';
import { usePolicyText } from './usePolicyText';

type DialogKind = null | 'apps' | 'models' | 'rate' | 'leader';

export function teamTitle(node: OrgTreeNode): string {
  return node.meta.team_display_name || node.name;
}

export function TeamDetail({
  node,
  breadcrumb,
  onOpenUser,
  onChanged,
}: {
  node: OrgTreeNode;
  /** 조직 › 부서 이름. */
  breadcrumb: string[];
  onOpenUser: (_id: string) => void;
  /** 트리 메타(팀장·개별 정책 점)를 다시 읽게 한다. */
  onChanged: () => void;
}) {
  const t = useTranslations('usersDetail');
  const tu = useTranslations('users');
  const tz = useReportingTz();
  const pit = usePointInTime();
  const tc = useTranslations('common');
  const period = useCurrentPeriod();
  const [dialog, setDialog] = useState<DialogKind>(null);
  const [usageGen, setUsageGen] = useState(0);
  const catalog = useCatalog();
  const [policy, reloadPolicy] = usePolicy('team', node.id);
  const usage = useTeamUsage(node.id, period, usageGen);
  const members = useMemo(() => node.children ?? [], [node.children]);
  const activity = useActivity(members.map((m) => m.id));
  const cat = catalog.status === 'ready' ? catalog.data : [];
  const text = usePolicyText(cat);
  const title = teamTitle(node);
  const memberCount = node.meta.member_count ?? members.length;
  const leaders = members.filter((m) => m.meta.role === 'TEAM_LEADER');
  const leaderText = leaders.length
    ? leaders.map((l) => userLabel({ email: l.meta.email, display_name: l.name })).join(', ')
    : t('team.noLeader');

  const afterSave = () => {
    reloadPolicy();
    onChanged();
  };

  const p = policy.status === 'ready' ? policy.data : null;
  const apps = p ? appsSummary(p, 'team') : null;
  const models = p && catalog.status === 'ready' ? modelsSummary(p, cat, 'team') : null;
  const rate = p ? rateSummary(p, 'team') : null;
  const ws = p ? webSearchSummary(p.web_search_quota, 'team') : null;
  const budget = p ? budgetSummary(p, 'team') : null;
  const down = p ? downgradeSummary(p, 'team') : null;

  // 사용량 — 표 행은 사용이 있는 구성원만 온다.
  const u = usage.status === 'ready' ? usage.data : null;
  const total = u?.total ?? EMPTY_CELL;
  const active = u ? members.filter((m) => (u.byUser.get(m.id)?.requests ?? 0) > 0).length : null;
  const budgetPct = budget?.max ? ((budget.used ?? Number(total.cost_usd)) / budget.max) * 100 : null;
  const wsLimit = ws?.monthlyLimit ?? null;

  const rows = useMemo(() => {
    const byUser = u?.byUser;
    return [...members]
      .map((m) => ({ m, c: byUser?.get(m.id) ?? EMPTY_CELL }))
      .sort(
        (a, b) =>
          Number(b.c.cost_usd) - Number(a.c.cost_usd) ||
          b.c.total_tokens - a.c.total_tokens ||
          userLabel({ email: a.m.meta.email, display_name: a.m.name }).localeCompare(
            userLabel({ email: b.m.meta.email, display_name: b.m.name }),
          ),
      );
  }, [members, u]);

  return (
    <div className="space-y-4" data-testid="team-detail">
      {/* 머리 */}
      <div>
        {breadcrumb.length > 0 && <p className="text-xs text-muted-foreground">{breadcrumb.join(' › ')}</p>}
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="break-all text-xl font-bold" data-testid="team-title">{title}</h2>
              <span className="badge badge-sky whitespace-nowrap">{t('team.chip')}</span>
            </div>
            <p className="mt-1 text-sm text-muted-foreground">
              {t('team.meta', { count: memberCount })} · {t('team.leader')}{' '}
              <strong className="font-semibold text-foreground">{leaderText}</strong>
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <button
              type="button"
              onClick={() => setDialog('leader')}
              className="inline-flex items-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              {t('team.changeLeader')}
            </button>
            <Link
              href={`/analytics?group_by=user&scope=${encodeURIComponent(`team:${node.id}`)}`}
              className="inline-flex items-center gap-1 rounded-md px-2 py-1.5 text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
            >
              {t('team.viewAnalytics')}
              <ArrowUpRight size={12} aria-hidden="true" />
            </Link>
          </div>
        </div>
      </div>

      {/* 사용량 카드 */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5" data-testid="team-kpis">
        {usage.status === 'loading' ? (
          <KpiSkeleton />
        ) : (
          <>
            <KpiCard
              label={t('kpi.cost')}
              value={u ? fmtUsd(Number(total.cost_usd)) : '—'}
              barPct={budgetPct}
              sub={
                budget?.max
                  ? t('kpi.ofTeamBudget', { pct: (budgetPct ?? 0).toFixed(1), amount: fmtUsd(budget.max) })
                  : t('kpi.noTeamBudget')
              }
            />
            <KpiCard label={t('kpi.tokens')} value={u ? fmtTokensCount(total.total_tokens) : '—'} sub={t('kpi.tokensSub')} />
            <KpiCard label={t('kpi.requests')} value={u ? total.requests.toLocaleString('en-US') : '—'} sub={t('kpi.thisMonth')} />
            <KpiCard
              label={t('kpi.webSearches')}
              value={ws ? t('kpi.searches', { count: ws.used }) : '—'}
              barPct={wsLimit && wsLimit > 0 && ws ? (ws.used / wsLimit) * 100 : null}
              sub={
                ws
                  ? wsLimit == null
                    ? t('kpi.teamPoolUnlimited')
                    : wsLimit === 0
                      ? t('kpi.teamPoolOff')
                      : t('kpi.teamPool', { limit: wsLimit.toLocaleString('en-US') })
                  : undefined
              }
            />
            <KpiCard
              label={t('kpi.activeUsers')}
              value={active == null ? '—' : `${active} / ${memberCount}`}
              sub={t('kpi.activeUsersSub')}
            />
          </>
        )}
      </div>
      {usage.status === 'failed' && <p className="text-xs text-destructive">{t('usageFailed')}</p>}

      {/* 적용 중인 정책 */}
      <Section title={t('policies.title')} aside={t('policies.legendTeam')} testId="team-policies">
        {policy.status === 'failed' ? (
          <p className="py-2 text-sm text-destructive">{t('policies.loadFailed')}</p>
        ) : !p ? (
          <div className="space-y-2 py-2" aria-busy="true">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="h-9 animate-pulse rounded-md bg-muted/60" />
            ))}
          </div>
        ) : (
          <>
            <PolicyRow
              testId="policy-apps"
              label={t('policies.apps')}
              value={text.apps(apps!)}
              source={apps!.source}
              scope="team"
              action={{ kind: 'button', label: t('policies.change'), onClick: () => setDialog('apps') }}
            />
            <PolicyRow
              testId="policy-models"
              label={t('policies.models')}
              value={models ? text.models(models).value : '…'}
              sub={models ? text.models(models).sub : null}
              source={models?.source ?? null}
              scope="team"
              action={{
                kind: 'button',
                label: t('policies.change'),
                onClick: () => setDialog('models'),
                disabled: catalog.status !== 'ready',
              }}
            />
            <PolicyRow
              testId="policy-rate"
              label={t('policies.rate')}
              value={text.rate(rate!.effective)}
              source={rate!.source}
              scope="team"
              action={{ kind: 'button', label: t('policies.change'), onClick: () => setDialog('rate') }}
            />
            <PolicyRow
              testId="policy-websearch"
              label={t('policies.webSearch')}
              value={ws ? text.webSearch(ws, 'team') : '—'}
              source={ws?.source ?? null}
              scope="team"
              action={{ kind: 'link', label: t('policies.inBudgets'), href: '/budgets#web-search-quota' }}
            />
            <PolicyRow
              testId="policy-budget"
              label={t('policies.budget')}
              value={text.budget(budget!, 'team').value}
              sub={text.budget(budget!, 'team').sub}
              source={budget!.source}
              scope="team"
              action={{ kind: 'link', label: t('policies.inBudgets'), href: `/budgets?team=${node.id}` }}
            />
            <PolicyRow
              testId="policy-downgrade"
              label={t('policies.downgrade')}
              value={text.downgrade(down!)}
              source={down!.source}
              scope="team"
              action={{ kind: 'link', label: t('policies.inBudgets'), href: `/budgets?team=${node.id}` }}
            />
          </>
        )}
      </Section>

      <RateLimitUsageCard scope="team" id={node.id} rate={rate} />

      {/* 구성원 */}
      <Section
        title={t('members.title', { count: memberCount })}
        aside={t('members.aside', { tz })}
        testId="team-members"
      >
        <div className="-mx-2">
          <Table density="compact">
            <THead>
              <Tr>
                <Th>{t('members.user')}</Th>
                <Th numeric>{t('members.cost')}</Th>
                <Th numeric>{t('members.tokens')}</Th>
                <Th numeric>{t('members.requests')}</Th>
                <Th numeric>{t('members.webSearches')}</Th>
                <Th>{t('members.ownPolicy')}</Th>
                <Th>{pit.header(t('members.lastUsed'))}</Th>
              </Tr>
            </THead>
            <TBody>
              {rows.length === 0 ? (
                <TEmpty colSpan={7}>{t('members.empty')}</TEmpty>
              ) : (
                rows.map(({ m, c }) => {
                  // 합성 이메일(<sub>@unknown)은 보이지 않는다 — 이름 + 미인증 배지.
                  const who = userNameParts(m.name, m.meta.email, tc('unnamedUser'));
                  const primary = who.primary;
                  const role = m.meta.role ? tu(`roleLabel.${m.meta.role}` as 'roleLabel.ADMIN') : null;
                  const act = activity.status === 'ready' ? activity.data.get(m.id) : undefined;
                  const open = () => onOpenUser(m.id);
                  return (
                    <Tr
                      key={m.id}
                      clickable
                      tabIndex={0}
                      data-member-id={m.id}
                      onClick={open}
                      onKeyDown={(e) => {
                        if (e.target !== e.currentTarget) return;
                        if (e.key === 'Enter' || e.key === ' ') {
                          e.preventDefault();
                          open();
                        }
                      }}
                      className="outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                    >
                      <Td className="max-w-[18rem]">
                        <div className="flex min-w-0 items-center gap-1.5">
                          <span className="truncate font-medium" title={primary}>{primary}</span>
                          {m.meta.role === 'TEAM_LEADER' && <span className="badge badge-sky whitespace-nowrap">{t('members.leaderBadge')}</span>}
                        </div>
                        <div className="flex min-w-0 items-center gap-1.5 text-xs text-muted-foreground">
                          <span className="truncate" title={who.name ? who.email ?? '' : ''}>
                            {[who.name ? who.email : null, role].filter(Boolean).join(' · ')}
                          </span>
                          {who.unverified && <UnverifiedEmailBadge />}
                        </div>
                      </Td>
                      <Td numeric>{u ? fmtUsd(Number(c.cost_usd)) : '—'}</Td>
                      <Td numeric>{u ? fmtTokensCount(c.total_tokens) : '—'}</Td>
                      <Td numeric>{u ? c.requests.toLocaleString('en-US') : '—'}</Td>
                      <Td numeric>{act ? act.web_searches.toLocaleString('en-US') : '—'}</Td>
                      <Td>
                        {m.meta.has_custom_policies ? (
                          <span className="badge badge-amber whitespace-nowrap">{t('members.ownYes')}</span>
                        ) : (
                          <span className="text-xs text-muted-foreground">—</span>
                        )}
                      </Td>
                      <Td className="whitespace-nowrap text-xs text-muted-foreground tabular-nums" title={pit.cellTitle(act?.last_used_at)}>
                        {act?.last_used_at ? pit.stamp(act.last_used_at) : '—'}
                      </Td>
                    </Tr>
                  );
                })
              )}
            </TBody>
          </Table>
        </div>
      </Section>

      <DangerZone node={node} title={title} memberCount={memberCount} />

      {p && (
        <>
          <AppAccessDialog
            isOpen={dialog === 'apps'}
            onClose={() => setDialog(null)}
            scope="team"
            id={node.id}
            target={title}
            inheritedText={
              apps!.source === 'org' ? text.apps(apps!) : apps!.source === 'none' ? text.apps({ allowed: null, source: 'none' }) : t('apps.orgPolicy')
            }
            onSaved={afterSave}
          />
          {catalog.status === 'ready' && (
            <AllowedModelsDialog
              isOpen={dialog === 'models'}
              onClose={() => setDialog(null)}
              scope="team"
              id={node.id}
              target={title}
              catalog={cat}
              onSaved={afterSave}
            />
          )}
          <RateLimitDialog
            isOpen={dialog === 'rate'}
            onClose={() => setDialog(null)}
            scope="team"
            id={node.id}
            target={title}
            org={rate!.org}
            onSaved={afterSave}
          />
        </>
      )}
      <LeaderDialog
        isOpen={dialog === 'leader'}
        onClose={() => setDialog(null)}
        team={node}
        target={title}
        onChanged={() => {
          onChanged();
          setUsageGen((g) => g + 1);
        }}
      />
    </div>
  );
}

function DangerZone({ node, title, memberCount }: { node: OrgTreeNode; title: string; memberCount: number }) {
  const t = useTranslations('users');
  const tc = useTranslations('common');
  const td = useTranslations('usersDetail');
  const { toast } = useToast();
  const [open, setOpen] = useState(false);
  const [pending, start] = useTransition();
  const run = () =>
    start(async () => {
      const r = await forceReauthTeamAction(node.id);
      setOpen(false);
      if (r.success) toast({ type: 'success', message: t('forceReauth.success', { count: r.data.revoked_count }), auto_dismiss_ms: 5000 });
      else toast({ type: 'error', message: r.error, auto_dismiss_ms: 4000 });
    });
  return (
    <section
      aria-labelledby={`danger-zone-${node.id}`}
      className="flex flex-wrap items-center justify-between gap-3 rounded-apple border border-destructive/30 px-4 py-3"
    >
      <div className="min-w-0">
        <h3 id={`danger-zone-${node.id}`} className="text-sm font-semibold text-destructive">
          {td('danger.title')}
        </h3>
        <p className="mt-0.5 text-xs text-muted-foreground">{t('forceReauth.dangerZoneDesc')}</p>
      </div>
      <SpinnerButton type="button" variant="dangerOutline" size="sm" onClick={() => setOpen(true)} disabled={memberCount === 0}>
        {t('forceReauth.button')}
      </SpinnerButton>
      <AppDialog isOpen={open} onClose={() => !pending && setOpen(false)} title={t('forceReauth.button')}>
        <p className="mb-2 text-sm text-muted-foreground">
          <span className="font-medium text-foreground">{title}</span> {t('forceReauth.warning', { count: memberCount })}
        </p>
        <p className="mb-4 text-sm text-muted-foreground">{t('forceReauth.note')}</p>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setOpen(false)}
            disabled={pending}
            className="rounded-md border px-3 py-1.5 text-sm hover:bg-muted"
          >
            {tc('cancel')}
          </button>
          <SpinnerButton type="button" variant="danger" isLoading={pending} onClick={run}>
            {t('forceReauth.proceed')}
          </SpinnerButton>
        </div>
      </AppDialog>
    </section>
  );
}
