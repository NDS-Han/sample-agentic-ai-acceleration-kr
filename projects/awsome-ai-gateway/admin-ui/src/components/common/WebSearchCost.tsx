'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AgentCore 웹 검색 비용 — 화면 공용 표시 규칙(모니터링 전용).
 *
 *  - 비용은 공용 fmtUsd/<Usd> 규칙. ``cost_usd === null`` 은 "Not tracked" 로 쓴다 —
 *    "$0.00" 으로 그리면 비용이 기록되지 않은 사용분이 무료였다는 거짓말이 된다.
 *  - 무료 사용량(조직 공유 월 5,000건)·정가 안내는 보이는 문장이 아니라 ⓘ 툴팁(WebSearchPriceInfo).
 *  - 언제 보일지는 lib/utils/webSearchVisibility 의 규칙을 호출자가 먼저 적용한다.
 */

import { useTranslations } from 'next-intl';

import { InfoTooltip } from '@/components/common/InfoTooltip';
import { Usd } from '@/components/common/Usd';
import { fmtUsd, fmtUsdRate, usdTitle } from '@/lib/utils/format';

/** 비용 값 하나 — 금액 또는 "Not tracked". ``significant`` 는 KPI 처럼 값 하나만 크게 보일 때. */
export function WebSearchCostValue({
  cost,
  significant = false,
  className,
}: {
  cost: number | null;
  significant?: boolean;
  className?: string;
}) {
  const t = useTranslations('webSearchCost');
  if (cost == null) {
    return (
      <span
        className={['text-muted-foreground', className].filter(Boolean).join(' ')}
        title={t('notTrackedHint')}
        data-web-search-cost="untracked"
      >
        {t('notTracked')}
      </span>
    );
  }
  if (significant) {
    return (
      <span className={className} title={usdTitle(cost)} data-web-search-cost="tracked">
        {fmtUsd(cost, { subCent: 'significant' })}
      </span>
    );
  }
  return (
    <span data-web-search-cost="tracked">
      <Usd value={cost} className={className} />
    </span>
  );
}

/** ⓘ — 조직 공유 월 5,000건 무료 + 정가 계산이라 실제 청구는 더 낮을 수 있음. */
export function WebSearchPriceInfo({
  price,
  align = 'left',
  side = 'bottom',
}: {
  /** USD / 1,000 queries(현재 단가). */
  price: number | string;
  align?: 'left' | 'center';
  side?: 'top' | 'bottom';
}) {
  const t = useTranslations('webSearchCost');
  return (
    <InfoTooltip label={t('infoLabel')} align={align} side={side}>
      <span data-testid="web-search-price-info">
        {t('freeTierTooltip', { price: fmtUsdRate(Number(price)) })}
      </span>
    </InfoTooltip>
  );
}

/** 한 줄 요약: "3 web searches · $0.02" (+ 비용 미기록 분) — 사용량 서브라인용. */
export function WebSearchInline({
  queries,
  cost,
  untracked = 0,
}: {
  queries: number;
  cost: number | null;
  untracked?: number;
}) {
  const t = useTranslations('webSearchCost');
  if (!(queries > 0)) return null;
  return (
    <span data-testid="web-search-inline">
      {t('searches', { count: queries })}
      {' · '}
      <WebSearchCostValue cost={cost} />
      {untracked > 0 && cost != null && (
        <span className="text-muted-foreground"> · {t('untrackedPart', { count: untracked })}</span>
      )}
    </span>
  );
}
