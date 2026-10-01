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

export function MyBudgetCard({ data }: { data: MyBudgetResponse }) {
  const t = useTranslations('my');
  const b = data.budget;
  // 개인 예산 없음 — 팀 예산/D-cap 적용. 한도·잔액·사용률은 백엔드가 null 로 내린다.
  const hasPersonalBudget = b.limit_usd != null;
  const pct = b.usage_pct ?? 0;
  const level = alertLevelOf(b.usage_pct);

  const policy =
    b.policy != null && KNOWN_POLICIES.includes(b.policy)
      ? t(`policyLabel.${b.policy}`)
      : b.policy;

  return (
    <div className="glass glass-hover rounded-apple p-6">
      <h2 className="text-base font-semibold mb-4">{t('monthlyBudget')}</h2>
      <div className="space-y-3">
        <div className="flex items-end justify-between">
          <span className="text-sm text-muted-foreground">{t('spent')}</span>
          <span className="text-2xl font-bold">{fmtUsd(b.used_usd)}</span>
        </div>

        {hasPersonalBudget ? (
          <>
            <UsageBar pct={pct} level={level} />
            <div className="flex items-center justify-between text-sm text-muted-foreground">
              <span>{t('limit', { amount: fmtUsd(b.limit_usd ?? 0).slice(1) })}</span>
              <span className={PCT_TONE[level] ?? 'text-muted-foreground'}>
                {pct.toFixed(1)}%
              </span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{t('remaining')}</span>
              <span className="font-medium">{fmtUsd(b.remaining_usd ?? 0)}</span>
            </div>
            <div className="flex items-center justify-between text-sm">
              <span className="text-muted-foreground">{t('overagePolicy')}</span>
              <span className="font-medium">{policy}</span>
            </div>
          </>
        ) : (
          <p className="text-sm text-muted-foreground">{t('teamBudgetApplied')}</p>
        )}
      </div>
    </div>
  );
}
