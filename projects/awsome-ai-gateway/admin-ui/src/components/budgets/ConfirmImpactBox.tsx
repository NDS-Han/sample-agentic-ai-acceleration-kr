'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import { AlertTriangle } from 'lucide-react';
import type { ConfirmationPayload } from '@/lib/actions/types';

interface WarningItem {
  impact?: string;
  [key: string]: unknown;
}

interface ConfirmImpactBoxProps {
  confirmation: ConfirmationPayload;
  isPending: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** admin-api 가 보내는 impact 코드 → i18n 키. 알 수 없는 코드는 그대로 노출한다. */
const IMPACT_KEYS: Record<string, string> = {
  team_blocked: 'confirmImpacts.teamBlocked',
  default_cap_blocks_members: 'confirmImpacts.defaultCapBlocksMembers',
  individual_budgets_cleared: 'confirmImpacts.individualBudgetsCleared',
  app_budgets_cascaded: 'confirmImpacts.appBudgetsCascaded',
  user_blocked: 'confirmImpacts.userBlocked',
  user_blocked_by_default_cap: 'confirmImpacts.userBlockedByDefaultCap',
  app_blocked: 'confirmImpacts.appBlocked',
};

function impactLabel(t: ReturnType<typeof useTranslations>, impact: string): string {
  const key = IMPACT_KEYS[impact];
  return key ? t(key) : impact;
}

/** 영향 필드(impact 제외)를 "key: value" 목록으로 평탄화 — 새 warning 필드가
 *  추가돼도 번역 없이 노출되도록 generic 렌더링을 쓴다. */
function warningLines(w: WarningItem): string[] {
  return Object.entries(w)
    .filter(([k]) => k !== 'impact')
    .map(([k, v]) => {
      const val = Array.isArray(v) ? v.map(String).join(', ') : String(v);
      return `${k}: ${val}`;
    });
}

export function ConfirmImpactBox({ confirmation, isPending, onConfirm, onCancel }: ConfirmImpactBoxProps) {
  const t = useTranslations('budgets');
  const tCommon = useTranslations('common');

  const details = confirmation.details as { warnings?: WarningItem[] } | undefined;
  const warnings = details?.warnings ?? [];

  return (
    <div
      role="alertdialog"
      aria-label={t('confirmImpactTitle')}
      className="rounded-md border border-amber-500/50 bg-amber-50 dark:bg-amber-950/30 p-3 space-y-2"
    >
      <div className="flex items-start gap-2">
        <AlertTriangle size={16} className="mt-0.5 shrink-0 text-amber-600" aria-hidden="true" />
        <div className="min-w-0">
          <p className="text-sm font-medium text-amber-800 dark:text-amber-200">
            {t('confirmImpactTitle')}
          </p>
          <p className="text-xs text-amber-700 dark:text-amber-300 mt-0.5 break-words">
            {confirmation.message}
          </p>
          {warnings.length > 0 && (
            <ul className="mt-1.5 space-y-1 text-xs text-amber-800 dark:text-amber-200">
              {warnings.map((w, i) => (
                <li key={i} className="rounded bg-amber-100/60 dark:bg-amber-900/40 px-2 py-1">
                  <span className="font-medium">
                    {w.impact ? impactLabel(t, w.impact) : ''}
                  </span>
                  {warningLines(w).length > 0 && (
                    <span className="block tabular-nums opacity-80">
                      {warningLines(w).join(' · ')}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </div>
      <div className="flex items-center justify-end gap-2 pt-1">
        <button
          type="button"
          onClick={onCancel}
          disabled={isPending}
          className="inline-flex items-center justify-center rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors disabled:opacity-50"
        >
          {tCommon('cancel')}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          disabled={isPending}
          className="inline-flex items-center justify-center rounded-md bg-amber-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-700 transition-colors disabled:opacity-50"
        >
          {isPending ? t('confirmProceeding') : t('confirmProceed')}
        </button>
      </div>
    </div>
  );
}
