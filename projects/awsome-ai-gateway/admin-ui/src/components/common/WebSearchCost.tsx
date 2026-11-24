'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AgentCore 웹 검색 비용 — 화면 공용 표시 규칙(모니터링 전용).
 *
 *  - 비용은 공용 fmtUsd/<Usd> 규칙. ``cost_usd === null`` 은 "미추적" 으로 쓴다 —
 *    "$0.00" 으로 그리면 추적 이전 사용분이 무료였다는 거짓말이 된다.
 *  - ``untracked_queries > 0`` 이면 비용은 추적된 부분의 합계라는 사실을 함께 적는다.
 *  - 정가 계산 + 조직 공유 월 5,000건 무료분 안내, 예산 제외 안내는 ``WebSearchCostNote``.
 */

import { useTranslations } from 'next-intl';

import { InfoTooltip } from '@/components/common/InfoTooltip';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { Usd } from '@/components/common/Usd';
import { fmtDateMinute, fmtUsd, usdTitle } from '@/lib/utils/format';
import type { WebSearchUsage } from '@/types/entities';

function useTrackingSince(ws: Pick<WebSearchUsage, 'tracking_since'> | null | undefined) {
  const tz = useReportingTz();
  return ws?.tracking_since ? fmtDateMinute(ws.tracking_since, tz) : null;
}

/** 비용 값 하나 — 금액 또는 "미추적". ``significant`` 는 KPI 처럼 값 하나만 크게 보일 때. */
export function WebSearchCostValue({
  cost,
  trackingSince,
  significant = false,
  className,
}: {
  cost: number | null;
  trackingSince?: string | null;
  significant?: boolean;
  className?: string;
}) {
  const t = useTranslations('webSearchCost');
  const since = useTrackingSince({ tracking_since: trackingSince ?? null });
  if (cost == null) {
    return (
      <span
        className={['text-muted-foreground', className].filter(Boolean).join(' ')}
        title={t('notTrackedHint', { since: since ?? 'none' })}
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

/** "추적 시작 전 N회 별도" — 기간 일부만 추적된 경우에만 그린다. */
export function WebSearchUntrackedPart({ ws, className }: { ws: WebSearchUsage; className?: string }) {
  const t = useTranslations('webSearchCost');
  if (!(ws.untracked_queries > 0) || ws.cost_usd == null) return null;
  return (
    <span className={['text-xs text-muted-foreground', className].filter(Boolean).join(' ')}>
      {t('untrackedPart', { count: ws.untracked_queries })}
    </span>
  );
}

/**
 * ⓘ 안내 — 정가 계산·조직 공유 무료분(실제 청구는 더 낮을 수 있음)·예산 제외·추적 시작 시각.
 * ``inline`` 은 아이콘 없이 문장으로(카드 하단 각주 등).
 */
export function WebSearchCostNote({
  ws,
  inline = false,
  align = 'left',
  side = 'bottom',
}: {
  ws?: Pick<WebSearchUsage, 'tracking_since'> | null;
  inline?: boolean;
  align?: 'left' | 'center';
  side?: 'top' | 'bottom';
}) {
  const t = useTranslations('webSearchCost');
  const since = useTrackingSince(ws);
  const tracking = since ? t('trackingSince', { date: since }) : t('trackingNotStarted');
  if (inline) {
    return (
      <p className="text-xs text-muted-foreground" data-testid="web-search-cost-note">
        {t('freeTierNote')} {t('excludedFromBudgets')} {tracking}.
      </p>
    );
  }
  return (
    <InfoTooltip label={t('noteLabel')} align={align} side={side}>
      <span className="block" data-testid="web-search-cost-note">{t('freeTierNote')}</span>
      <span className="mt-1 block">{t('excludedFromBudgets')}</span>
      <span className="mt-1 block">{tracking}</span>
    </InfoTooltip>
  );
}

/** 한 줄 요약: "웹 검색 3회 · $0.021" (+ 미추적 분) — 앱 점유·사용량 서브라인용. */
export function WebSearchInline({
  queries,
  cost,
  untracked = 0,
  trackingSince,
}: {
  queries: number;
  cost: number | null;
  untracked?: number;
  trackingSince?: string | null;
}) {
  const t = useTranslations('webSearchCost');
  if (!(queries > 0)) return null;
  return (
    <span data-testid="web-search-inline">
      {t('perRequest', { count: queries })}
      {' · '}
      <WebSearchCostValue cost={cost} trackingSince={trackingSince} />
      {untracked > 0 && cost != null && (
        <span className="text-muted-foreground"> · {t('untrackedPart', { count: untracked })}</span>
      )}
    </span>
  );
}
