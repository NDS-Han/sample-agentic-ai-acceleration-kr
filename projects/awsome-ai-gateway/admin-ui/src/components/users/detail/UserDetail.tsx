'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 사용자 상세 — 머리(이름·이메일·역할·팀·마지막 사용, Analytics·Body Logs·API Keys 바로가기),
 * 사용량 카드 5장, 모델별 사용, 적용 중인 정책(출처: 개별 지정 / 팀 정책 상속 / 조직 기본값),
 * 실제 적용 결과(유효 정책). 편집은 항목별 대화상자.
 */

import { useState } from 'react';
import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ArrowUpRight } from 'lucide-react';

import { RoleBadge } from '@/components/common/Badge';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateMinute, fmtTokensCompact, fmtUsd } from '@/lib/utils/format';
import { modelDisplay } from '@/lib/utils/modelLabel';
import type { OrgTreeNode } from '@/types/entities';

import { AllowedModelsDialog } from './AllowedModelsDialog';
import { AppAccessDialog } from './AppAccessDialog';
import { EffectivePolicySummary } from './EffectivePolicySummary';
import { KpiCard, KpiSkeleton, PolicyRow, Section } from './parts';
import { appsSummary, budgetSummary, modelsSummary, rateSummary, webSearchSummary } from './policyRows';
import { RateLimitDialog } from './RateLimitDialog';
import { EMPTY_CELL, useActivity, useCatalog, useCurrentPeriod, usePolicy, useTeamUsage, useUserUsage } from './useDetailData';
import { usePolicyText } from './usePolicyText';

type DialogKind = null | 'apps' | 'models' | 'rate';

export function userTitle(node: OrgTreeNode): { title: string; email: string | null; hasName: boolean } {
  const email = node.meta.email ?? null;
  const hasName = !!node.name && node.name !== email;
  return { title: hasName ? node.name : (email ?? node.name), email, hasName };
}

export function UserDetail({
  node,
  teamId,
  breadcrumb,
  onChanged,
}: {
  node: OrgTreeNode;
  /** 소속 팀 id(트리 부모). 없으면 팀 대비 지표는 '—'. */
  teamId: string | null;
  breadcrumb: string[];
  onChanged: () => void;
}) {
  const t = useTranslations('usersDetail');
  const tu = useTranslations('users');
  const tz = useReportingTz();
  const period = useCurrentPeriod();
  const [dialog, setDialog] = useState<DialogKind>(null);
  const catalog = useCatalog();
  const [policy, reloadPolicy] = usePolicy('user', node.id);
  const usage = useUserUsage(node.id, period);
  const teamUsage = useTeamUsage(teamId, period);
  const activity = useActivity([node.id]);
  const cat = catalog.status === 'ready' ? catalog.data : [];
  const text = usePolicyText(cat);
  const { title, email, hasName } = userTitle(node);
  const role = node.meta.role;
  const teamName = node.meta.team_display_name || node.meta.team_name || t('user.noTeam');
  const act = activity.status === 'ready' ? activity.data.get(node.id) : undefined;
  const lastUsed = act ? (act.last_used_at ? fmtDateMinute(act.last_used_at, tz) : t('user.never')) : '—';

  const p = policy.status === 'ready' ? policy.data : null;
  const apps = p ? appsSummary(p, 'user') : null;
  const models = p && catalog.status === 'ready' ? modelsSummary(p, cat, 'user') : null;
  const rate = p ? rateSummary(p, 'user') : null;
  const ws = p ? webSearchSummary(p.web_search_quota, 'user') : null;
  const budget = p ? budgetSummary(p, 'user') : null;

  const d = usage.status === 'ready' ? usage.data : null;
  const total = d?.total ?? (usage.status === 'failed' ? null : EMPTY_CELL);
  const teamCost = teamUsage.status === 'ready' ? Number(teamUsage.data.total.cost_usd) : null;
  const share = total && teamCost && teamCost > 0 ? (Number(total.cost_usd) / teamCost) * 100 : null;
  const teamRemaining = budget?.teamMax != null ? budget.teamMax - (budget.teamUsed ?? 0) : null;
  const teamPct = budget?.teamMax ? ((budget.teamUsed ?? 0) / budget.teamMax) * 100 : null;
  const modelsList = [...(d?.models ?? [])].sort((a, b) => b.total_tokens - a.total_tokens);
  const maxTok = Math.max(1, ...modelsList.map((m) => m.total_tokens));

  const afterSave = () => {
    reloadPolicy();
    onChanged();
  };

  const link = (href: string, label: string) => (
    <Link
      href={href}
      className="inline-flex items-center gap-1 rounded-md border border-border bg-background px-2.5 py-1.5 text-xs font-medium hover:bg-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
    >
      {label}
      <ArrowUpRight size={12} aria-hidden="true" />
    </Link>
  );

  return (
    <div className="space-y-4" data-testid="user-detail">
      <div>
        {breadcrumb.length > 0 && <p className="text-xs text-muted-foreground">{breadcrumb.join(' › ')}</p>}
        <div className="mt-1 flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <h2 className="break-all text-xl font-bold" data-testid="user-title">{title}</h2>
            <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-sm text-muted-foreground">
              {hasName && email && <span className="break-all">{email}</span>}
              {role && <RoleBadge role={role} label={tu(`roleLabel.${role}` as 'roleLabel.ADMIN')} />}
              <span>
                {teamName} · {t('user.lastUsed', { when: lastUsed })}
              </span>
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-2">
            {link(`/analytics?group_by=user&user=${encodeURIComponent(node.id)}`, t('user.analytics'))}
            {email && link(`/monitoring/body-logs?user=${encodeURIComponent(email)}`, t('user.bodyLogs'))}
            {email && link(`/keys?email=${encodeURIComponent(email)}`, t('user.apiKeys'))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-5" data-testid="user-kpis">
        {usage.status === 'loading' ? (
          <KpiSkeleton />
        ) : (
          <>
            <KpiCard
              label={t('kpi.cost')}
              value={total ? fmtUsd(Number(total.cost_usd)) : '—'}
              sub={share != null ? t('kpi.shareOfTeam', { pct: share.toFixed(0) }) : t('kpi.thisMonth')}
            />
            <KpiCard label={t('kpi.tokens')} value={total ? fmtTokensCompact(total.total_tokens) : '—'} sub={t('kpi.tokensSub')} />
            <KpiCard label={t('kpi.requests')} value={total ? total.requests.toLocaleString('en-US') : '—'} sub={t('kpi.thisMonth')} />
            <KpiCard
              label={t('kpi.webSearches')}
              value={ws ? t('kpi.searches', { count: ws.used }) : '—'}
              barPct={ws?.monthlyLimit ? (ws.used / ws.monthlyLimit) * 100 : null}
              sub={
                ws
                  ? ws.monthlyLimit == null
                    ? t('kpi.personalUnlimited', { cap: ws.perRequestCap })
                    : ws.monthlyLimit === 0
                      ? t('kpi.personalOff')
                      : t('kpi.personalLimit', { limit: ws.monthlyLimit.toLocaleString('en-US'), cap: ws.perRequestCap })
                  : undefined
              }
            />
            <KpiCard
              label={t('kpi.teamBudgetLeft')}
              value={teamRemaining != null ? fmtUsd(teamRemaining) : '—'}
              barPct={teamPct}
              sub={
                budget?.teamMax != null
                  ? t('kpi.teamBudgetOf', { amount: fmtUsd(budget.teamMax), pct: (teamPct ?? 0).toFixed(1) })
                  : t('kpi.noTeamBudget')
              }
            />
          </>
        )}
      </div>
      {usage.status === 'failed' && <p className="text-xs text-destructive">{t('usageFailed')}</p>}

      {modelsList.length > 0 && (
        <Section title={t('user.byModel')} aside={t('kpi.tokensSub')} testId="user-models">
          <ul className="space-y-2">
            {modelsList.map((m) => (
              <li key={m.model} className="grid grid-cols-[minmax(0,12rem)_minmax(0,1fr)_auto_auto_auto] items-center gap-3 text-sm">
                <span className="truncate font-medium" title={m.model}>{modelDisplay(m.model, m.display_name)}</span>
                <span className="h-1.5 overflow-hidden rounded-full bg-muted" aria-hidden="true">
                  <span className="block h-full rounded-full bg-primary" style={{ width: `${(m.total_tokens / maxTok) * 100}%` }} />
                </span>
                <span className="w-14 text-right tabular-nums">{fmtTokensCompact(m.total_tokens)}</span>
                <span className="w-16 text-right tabular-nums">{fmtUsd(Number(m.cost_usd))}</span>
                <span className="w-20 text-right text-xs text-muted-foreground tabular-nums">
                  {t('user.requests', { count: m.requests })}
                </span>
              </li>
            ))}
          </ul>
        </Section>
      )}

      <Section title={t('policies.title')} aside={t('policies.legendUser')} testId="user-policies">
        {policy.status === 'failed' ? (
          <p className="py-2 text-sm text-destructive">{t('policies.loadFailed')}</p>
        ) : !p ? (
          <div className="space-y-2 py-2" aria-busy="true">
            {Array.from({ length: 5 }, (_, i) => (
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
              scope="user"
              action={{ kind: 'button', label: t('policies.change'), onClick: () => setDialog('apps') }}
            />
            <PolicyRow
              testId="policy-models"
              label={t('policies.models')}
              value={models ? text.models(models).value : '…'}
              sub={models ? text.models(models).sub : null}
              source={models?.source ?? null}
              scope="user"
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
              scope="user"
              action={{ kind: 'button', label: t('policies.change'), onClick: () => setDialog('rate') }}
            />
            <PolicyRow
              testId="policy-websearch"
              label={t('policies.webSearch')}
              value={ws ? text.webSearch(ws, 'user') : '—'}
              sub={ws ? t('policies.wsWithTeam') : null}
              source={ws?.source ?? null}
              scope="user"
              action={{ kind: 'link', label: t('policies.inBudgets'), href: '/budgets#web-search-quota' }}
            />
            <PolicyRow
              testId="policy-budget"
              label={t('policies.budget')}
              value={text.budget(budget!, 'user').value}
              sub={text.budget(budget!, 'user').sub}
              source={budget!.source}
              scope="user"
              action={{ kind: 'link', label: t('policies.inBudgets'), href: `/budgets?user=${node.id}` }}
            />
          </>
        )}
      </Section>

      <Section title={t('effective.title')} aside={t('effective.aside')} testId="user-effective">
        {policy.status === 'failed' ? (
          <p className="py-2 text-sm text-destructive">{t('policies.loadFailed')}</p>
        ) : p ? (
          <EffectivePolicySummary policy={p} catalog={cat} />
        ) : (
          <div className="h-24 animate-pulse rounded-md bg-muted/60" aria-busy="true" />
        )}
      </Section>

      {p && (
        <>
          <AppAccessDialog
            isOpen={dialog === 'apps'}
            onClose={() => setDialog(null)}
            scope="user"
            id={node.id}
            target={`${title} · ${teamName}`}
            inheritedText={apps!.source === 'own' ? t('apps.teamPolicy') : text.apps(apps!)}
            onSaved={afterSave}
          />
          {catalog.status === 'ready' && (
            <AllowedModelsDialog
              isOpen={dialog === 'models'}
              onClose={() => setDialog(null)}
              scope="user"
              id={node.id}
              target={`${title} · ${teamName}`}
              catalog={cat}
              inherited={p.allowed_models_source === 'user' ? null : p.allowed_models}
              inheriting={p.allowed_models_source !== 'user'}
              onSaved={afterSave}
            />
          )}
          <RateLimitDialog
            isOpen={dialog === 'rate'}
            onClose={() => setDialog(null)}
            scope="user"
            id={node.id}
            target={`${title} · ${teamName}`}
            team={rate!.team}
            org={rate!.org}
            onSaved={afterSave}
          />
        </>
      )}
    </div>
  );
}
