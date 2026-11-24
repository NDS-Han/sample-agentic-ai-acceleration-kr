'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** 정책 요약 값 문구 — 팀·사용자 상세와 대화상자가 같은 표현을 쓴다. */

import { useCallback, useMemo } from 'react';
import { useTranslations } from 'next-intl';

import { CLIENTS } from '@/lib/constants/gateway';
import { fmtUsd } from '@/lib/utils/format';
import { labelFor, modelDisplay } from '@/lib/utils/modelLabel';
import type { ModelListItem } from '@/types/entities';
import {
  RATE_KEYS,
  rateIsUnlimited,
  type AppsSummary,
  type BudgetSummary,
  type DowngradeSummary,
  type ModelsSummary,
  type RateValues,
  type WebSearchSummary,
} from './policyRows';

export function usePolicyText(catalog: ModelListItem[]) {
  const t = useTranslations('usersDetail.value');
  const names = useMemo(() => new Map(catalog.map((m) => [m.alias, m.display_name])), [catalog]);
  const model = useCallback((alias: string) => modelDisplay(alias, names.get(alias) ?? null), [names]);

  return useMemo(
    () => ({
      model,
      apps(s: AppsSummary): string {
        if (s.allowed == null) return t('appsAll', { count: CLIENTS.length });
        if (CLIENTS.every((c) => s.allowed?.includes(c))) return t('appsAllExplicit', { count: CLIENTS.length });
        return t('appsSome', { apps: s.allowed.map((c) => labelFor(c, c)).join(' · ') });
      },
      models(s: ModelsSummary): { value: string; sub: string | null } {
        const value =
          s.blocked.length === 0
            ? t('modelsAll', { total: s.total })
            : t('modelsSome', { allowed: s.allowedCount, total: s.total });
        if (s.blocked.length === 0) return { value, sub: null };
        const shown = s.blocked.slice(0, 2).map(model);
        const rest = s.blocked.length - shown.length;
        return {
          value,
          sub: rest > 0 ? t('blockedSomeMore', { models: shown.join(', '), count: rest }) : t('blockedSome', { models: shown.join(', ') }),
        };
      },
      rate(v: RateValues): string {
        if (rateIsUnlimited(v)) return t('rateNone');
        return RATE_KEYS.filter((k) => v[k] != null)
          .map((k) =>
            k === 'rpm'
              ? `RPM ${v.rpm}`
              : k === 'tpm'
                ? `TPM ${Number(v.tpm).toLocaleString('en-US')}`
                : k === 'cpm'
                  ? t('perMin', { amount: `$${v.cpm}` })
                  : t('perHour', { amount: `$${v.cph}` }),
          )
          .join(' · ');
      },
      webSearch(s: WebSearchSummary, scope: 'team' | 'user'): string {
        const lim =
          s.monthlyLimit == null ? t('unlimited') : s.monthlyLimit === 0 ? t('off') : s.monthlyLimit.toLocaleString('en-US');
        return scope === 'team'
          ? t('wsTeam', { limit: lim, cap: s.perRequestCap })
          : t('wsUser', { limit: lim, cap: s.perRequestCap });
      },
      budget(s: BudgetSummary, scope: 'team' | 'user'): { value: string; sub: string | null } {
        if (scope === 'team') {
          if (s.max == null) return { value: t('budgetNone'), sub: null };
          const pct = s.max > 0 && s.used != null ? (s.used / s.max) * 100 : null;
          return {
            value: t('budgetMonthly', { amount: fmtUsd(s.max) }),
            sub: pct != null ? t('budgetUsedPct', { pct: pct.toFixed(1) }) : null,
          };
        }
        return {
          value: s.max != null ? t('budgetMonthly', { amount: fmtUsd(s.max) }) : t('budgetNoOwn'),
          sub: s.teamMax != null ? t('budgetSharesTeam', { amount: fmtUsd(s.teamMax) }) : t('budgetNoTeam'),
        };
      },
      downgrade(s: DowngradeSummary): string {
        if (s.steps.length === 0) return t('downgradeNone');
        return s.steps.map((st) => t('downgradeStep', { pct: st.threshold, model: model(st.to) })).join(' · ');
      },
    }),
    [model, t],
  );
}

export type PolicyText = ReturnType<typeof usePolicyText>;
