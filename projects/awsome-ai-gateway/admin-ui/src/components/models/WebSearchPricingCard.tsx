'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AgentCore 웹 검색 단가(USD / 1,000 queries) 편집 — Model Management 의 모델 단가 옆.
 *
 * **모니터링 전용**: 이 단가로 계산한 비용은 예산·HARD_BLOCK·요청 한도(CPM/CPH)에
 * 들어가지 않는다. 바꾸면 그 뒤 **기록되는** 사용분부터 적용되고(워커 캐시 ~30초),
 * 이미 기록된 행은 기록 당시 단가 그대로다. 변경은 감사 로그에 before/after 로 남는다.
 */

import { useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';

import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { WebSearchCostNote } from '@/components/common/WebSearchCost';
import { setWebSearchPricingAction, type WebSearchPricingSetting } from '@/lib/actions/settings';
import { fmtDateMinute, fmtUsdRate } from '@/lib/utils/format';
import { isValidWebSearchPrice } from '@/lib/utils/webSearchPrice';

export function WebSearchPricingCard({ initial }: { initial: WebSearchPricingSetting | null }) {
  const t = useTranslations('webSearchCost');
  const tz = useReportingTz();
  const { toast } = useToast();
  const [setting, setSetting] = useState(initial);
  const [draft, setDraft] = useState(initial ? String(Number(initial.price_per_1k_queries)) : '');
  const [isPending, startTransition] = useTransition();

  if (!setting) {
    return (
      <section className="glass rounded-apple p-4" data-testid="web-search-pricing-card">
        <h2 className="text-sm font-semibold">{t('pricingTitle')}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{t('pricingLoadFailed')}</p>
      </section>
    );
  }

  const valid = isValidWebSearchPrice(draft);
  const unchanged = valid && Number(draft) === Number(setting.price_per_1k_queries);

  const save = () => {
    if (!valid || unchanged) return;
    startTransition(async () => {
      const result = await setWebSearchPricingAction(draft.trim());
      if (result.success) {
        setSetting(result.data);
        setDraft(String(Number(result.data.price_per_1k_queries)));
        toast({ type: 'success', message: t('pricingSaved'), auto_dismiss_ms: 3000 });
      } else {
        toast({ type: 'error', message: `${t('pricingSaveFailed')} ${result.error}`, auto_dismiss_ms: 4000 });
      }
    });
  };

  return (
    <section className="glass rounded-apple p-4 space-y-3" data-testid="web-search-pricing-card">
      <div className="flex items-center gap-1.5">
        <h2 className="text-sm font-semibold">{t('pricingTitle')}</h2>
        <WebSearchCostNote ws={setting} />
      </div>
      <p className="text-xs text-muted-foreground">{t('pricingDesc')}</p>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          save();
        }}
      >
        <div className="flex flex-col gap-1">
          <label className="text-xs font-medium" htmlFor="web_search_price">
            {t('pricingLabel')}
          </label>
          <span className="relative">
            <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-sm text-muted-foreground">
              $
            </span>
            <input
              id="web_search_price"
              inputMode="decimal"
              autoComplete="off"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              aria-invalid={!valid || undefined}
              aria-describedby="web_search_price_help"
              className="w-36 rounded-apple-md border border-input bg-background py-1.5 pl-6 pr-3 text-sm tabular-nums focus:outline-none focus:ring-1 focus:ring-ring"
            />
          </span>
        </div>
        <SpinnerButton type="submit" size="sm" isLoading={isPending} disabled={!valid || unchanged}>
          {t('pricingSave')}
        </SpinnerButton>
      </form>
      <div id="web_search_price_help" className="space-y-0.5 text-xs text-muted-foreground">
        {!valid && <p className="text-destructive" role="alert">{t('pricingInvalid')}</p>}
        <p>
          {setting.is_default
            ? t('pricingUsingDefault')
            : t('pricingDefault', { price: fmtUsdRate(Number(setting.default_price_per_1k_queries)) })}
          {setting.updated_at && !setting.is_default
            ? ` · ${t('pricingLastUpdated', { date: fmtDateMinute(setting.updated_at, tz) })}`
            : ''}
        </p>
        <p>{t('pricingApplies')}</p>
      </div>
    </section>
  );
}
