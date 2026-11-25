'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { AlertTriangle } from 'lucide-react';
import { useTranslations } from 'next-intl';

import { Badge } from '@/components/common/Badge';
import { usePointInTime } from '@/components/common/usePointInTime';

/** admin-api 하이브리드 롤업 읽기 메타(services/usage_rollup). */
export interface FreshnessMeta {
  /** 사용한 롤업 중 가장 늦은 집계 시각(ISO). null/없음 = 롤업을 쓰지 않음(전부 실시간). */
  data_as_of?: string | null;
  /** 롤업이 지연·실패해 원본에서 직접 읽은 과거 날짜(YYYY-MM-DD, 리포팅 TZ). */
  delayed_days?: string[] | null;
}

/**
 * 'As of 14:05 GMT+9 (today live)' / '집계 기준 14:05 GMT+9 · 오늘분 실시간 반영' 라벨 +
 * 지연 날짜가 있으면 경고 칩.
 *
 * 시각은 다른 시점 표기와 같은 규칙 — 브라우저 TZ + 그 시점의 TZ 이름(usePointInTime).
 * 마운트 전(SSR)에는 시각을 그리지 않는다(파드 TZ 출력·하이드레이션 불일치 방지).
 * 지연 날짜는 리포팅 TZ 날짜(YYYY-MM-DD)라 그대로 보여준다.
 */
export function DataFreshness({ meta, className = '' }: { meta: FreshnessMeta | null | undefined; className?: string }) {
  const t = useTranslations('common.freshness');
  const pit = usePointInTime();
  if (!meta) return null;
  const delayed = meta.delayed_days ?? [];
  const asOf = meta.data_as_of ?? null;
  const hm = asOf ? pit.hmZ(asOf) : '';
  if (!pit.tz && delayed.length === 0) return null;

  return (
    <div
      className={`flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground ${className}`}
      data-testid="data-freshness"
    >
      {pit.tz && (
        <span title={t('hint')}>
          {asOf ? t('asOf', { time: hm }) : t('live')}
        </span>
      )}
      {delayed.length > 0 && (
        <Badge tone="amber" className="inline-flex items-center gap-1">
          <AlertTriangle className="h-3 w-3" aria-hidden />
          <span title={t('delayedHint', { days: delayed.join(', ') })}>
            {t('delayed', { count: delayed.length })}
          </span>
        </Badge>
      )}
    </div>
  );
}
