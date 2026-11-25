// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Total (charged) — 청구 총액과 분해(wave 3 DECISION 1/2).
 *
 * 총액 = 성공 + 실패(에러·타임아웃) + 취소 + 웹 검색. 프로바이더가 토큰을 처리했으면 상태와 무관하게
 * 청구된다(Bedrock 도달 전 거절은 $0 이라 여기에 없다). 웹 검색 null = 이 기간 추적된 행이 없음
 * ("Not tracked" — $0 이 아니다). 대시보드와 Analytics 가 같은 컴포넌트를 쓴다.
 */

import { getTranslations } from 'next-intl/server';

import type { ChargedBreakdown as Charged } from '@/types/entities';
import { fmtUsd, usdTitle } from '@/lib/utils/format';

export async function ChargedBreakdown({ charged }: { charged: Charged | null | undefined }) {
  if (!charged) return null;
  const t = await getTranslations('charged');
  const usd = (n: number) => fmtUsd(n, { subCent: 'significant' });
  const lines: { key: string; label: string; value: number | null }[] = [
    { key: 'success', label: t('success'), value: charged.success_usd },
    { key: 'failed', label: t('failed'), value: charged.failed_usd },
    { key: 'cancelled', label: t('cancelled'), value: charged.cancelled_usd },
    { key: 'webSearch', label: t('webSearch'), value: charged.web_search_usd },
  ];
  return (
    <section
      className="glass rounded-apple p-4 flex flex-col gap-3"
      data-testid="charged-breakdown"
      aria-label={t('title')}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="text-sm font-medium text-muted-foreground">{t('title')}</p>
        <p className="text-2xl font-bold" title={usdTitle(charged.total_usd)} data-testid="charged-total">
          {usd(charged.total_usd)}
        </p>
      </div>
      <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-4">
        {lines.map((l) => (
          <div key={l.key} className="flex flex-col" data-testid={`charged-${l.key}`}>
            <dt className="text-xs text-muted-foreground">{l.label}</dt>
            <dd className="font-medium" title={l.value == null ? t('notTrackedHint') : usdTitle(l.value)}>
              {l.value == null ? t('notTracked') : usd(l.value)}
            </dd>
          </div>
        ))}
      </dl>
      <p className="text-xs text-muted-foreground">{t('hint')}</p>
    </section>
  );
}
