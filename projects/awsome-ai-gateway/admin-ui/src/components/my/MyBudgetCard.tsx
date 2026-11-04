'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useTranslations } from 'next-intl';
import type { MyBudgetResponse } from '@/lib/actions/my';
import { fmtUsd } from '@/lib/utils/format';
import { UsageBar } from '@/components/budgets/budgetVisuals';
import { alertLevelOf } from '@/lib/utils/alertLevel';
import { AlertLevel } from '@/types/enums';

const KNOWN_POLICIES = ['HARD_BLOCK', 'SOFT_WARNING', 'THROTTLE'];

const PCT_TONE: Record<string, string> = {
  [AlertLevel.NORMAL]: 'text-[hsl(var(--chart-1))]',
  [AlertLevel.WARNING]: 'text-[hsl(38_92%_50%)]',
  [AlertLevel.CRITICAL]: 'text-destructive',
};

export function MyBudgetCard({
  data,
  basisMismatch = false,
}: {
  data: MyBudgetResponse;
  /** 사용량 섹션이 과거 월을 보고 있을 때 — 이 카드는 항상 이번 달 기준이라 기준 월을 표시한다. */
  basisMismatch?: boolean;
}) {
  const t = useTranslations('my');
  const b = data.budget;
  const team = data.team;

  // 실효 개인 상한: 개인 A_u 우선, 없으면 팀 기본 cap(D) 이 본인에게 적용된다.
  const personalCap = b.limit_usd;
  const teamDefaultCap = team?.default_cap_usd ?? null;
  const effectiveCap = personalCap ?? teamDefaultCap;
  const capIsTeamDefault = personalCap == null && teamDefaultCap != null;
  // 개인 예산 없음 — 팀 예산/D-cap 적용. 한도·잔액·사용률은 백엔드가 null 로 내린다.
  const hasPersonalBudget = b.limit_usd != null;
  // limit=0 은 실제로 저장된 $0 예산 — 요청이 전부 차단되는 상태다. usage_pct 는
  // null 로 내려오는데 ?? 0 으로 뭉개면 차단된 사용자에게 "0.0% NORMAL" 게이지가
  // 보인다(R25-N-UI2). BudgetSummaryTable 의 limit===0 특수 처리와 같은 구분.
  const isZeroBudget = b.limit_usd === 0;
  // D-cap 이 적용될 때 사용률은 백엔드 usage_pct 대신 로컬 계산 — budget.usage_pct
  // 는 개인 config 기준이라 null 이다.
  const capPct = hasPersonalBudget
    ? (b.usage_pct ?? 0)
    : effectiveCap
      ? (b.used_usd / effectiveCap) * 100
      : 0;
  const capRemaining = hasPersonalBudget
    ? (b.remaining_usd ?? 0)
    : effectiveCap != null
      ? effectiveCap - b.used_usd
      : 0;
  const level = alertLevelOf(hasPersonalBudget ? b.usage_pct : capPct);

  const policy =
    b.policy != null && KNOWN_POLICIES.includes(b.policy)
      ? t(`policyLabel.${b.policy}`)
      : b.policy;

  const teamLevel = alertLevelOf(team?.usage_pct ?? null);

  return (
    <div className="glass glass-hover rounded-apple p-6 h-full">
      <div className="mb-4 flex items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold">{t('monthlyBudget')}</h2>
        {basisMismatch && (
          <span className="text-xs text-muted-foreground">
            {t('budgetBasis', { period: data.period })}
          </span>
        )}
      </div>
      <div className="space-y-3">
        <div className="flex items-end justify-between">
          <span className="text-sm text-muted-foreground">{t('spent')}</span>
          <span className="text-2xl font-bold">{fmtUsd(b.used_usd)}</span>
        </div>

        {isZeroBudget ? (
          <div>
            <p className="text-sm font-medium text-[hsl(38_92%_40%)]">
              {t('zeroBudget')}
            </p>
            <p className="mt-1 text-xs text-muted-foreground">
              {t('zeroBudgetHint')}
            </p>
          </div>
        ) : effectiveCap != null ? (
          <>
            <UsageBar pct={capPct} level={level} />
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>
                {capIsTeamDefault
                  ? t('limitTeamDefault', { amount: fmtUsd(effectiveCap).slice(1) })
                  : t('limit', { amount: fmtUsd(effectiveCap).slice(1) })}
              </span>
              <span className={PCT_TONE[level] ?? 'text-muted-foreground'}>
                {capPct.toFixed(1)}%
              </span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{t('remaining')}</span>
              <span className="font-medium">{fmtUsd(capRemaining)}</span>
            </div>
            {hasPersonalBudget && (
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">{t('overagePolicy')}</span>
                <span className="font-medium">{policy}</span>
              </div>
            )}
          </>
        ) : (
          <p className="text-sm text-muted-foreground">
            {team ? t('noPersonalCapShared') : t('noCapNoTeam')}
          </p>
        )}

        {team && (
          <div className="border-t border-border/60 pt-3 space-y-2.5">
            <div className="flex items-center justify-between text-sm">
              <span className="font-medium">
                {t('teamPool', { name: team.team_name ? ` (${team.team_name})` : '' })}
              </span>
              {team.limit_usd != null && (
                <span className="text-muted-foreground">
                  {fmtUsd(team.used_usd)} / {fmtUsd(team.limit_usd)}
                </span>
              )}
            </div>
            {team.limit_usd == null ? (
              <p className="text-xs font-medium text-[hsl(38_92%_40%)]">
                {t('teamUnset')}
              </p>
            ) : (
              <>
                <UsageBar pct={team.usage_pct ?? 0} level={teamLevel} />
                <div className="flex items-center justify-between text-sm">
                  <span className="text-muted-foreground">{t('teamRemaining')}</span>
                  <span className={`font-medium ${PCT_TONE[teamLevel] ?? ''}`}>
                    {fmtUsd(team.remaining_usd ?? 0)}
                    <span className="ml-1 text-xs font-normal">
                      ({(team.usage_pct ?? 0).toFixed(1)}%)
                    </span>
                  </span>
                </div>
                <p className="text-xs text-muted-foreground">{t('teamSharedNote')}</p>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
