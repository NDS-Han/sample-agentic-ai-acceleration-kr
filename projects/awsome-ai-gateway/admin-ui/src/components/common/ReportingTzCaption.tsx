'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';

import { useDisplayTz, useIsClient, useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { tzZoneLabel, tzZoneText } from '@/lib/utils/timezone';

/**
 * 리포팅 TZ(REPORTING_TIMEZONE, 배포 리전) 기준으로 나뉜 차트·표의 캡션 —
 * 'Dates in IST (Asia/Kolkata)', 'Dates in ET (America/New_York)'. 시점 시각은 브라우저
 * TZ 라 두 시계를 헷갈리지 않게 버킷 기준을 한 번 적는다. 이름은 env 의 IANA TZ 에서
 * 유도(하드코딩 없음)하고 DST 독립이다 — 'EST'/'EDT' 같은 계절 약어를 쓰지 않는다.
 * 약어가 날짜(서머타임)에 따라 달라 SSR 과 어긋나지 않도록 마운트 후에만 그린다.
 */
export function ReportingTzCaption({
  kind = 'dates',
  className = '',
}: {
  /** dates=일별 축, hours=시간 축, period=월(예산·기간) 경계,
   *  dateOnly=표 안의 시각 없는 날짜 열(YYYY-MM-DD, 키 만료일 등). */
  kind?: 'dates' | 'hours' | 'period' | 'dateOnly';
  className?: string;
}) {
  const t = useTranslations('common.reportingTz');
  const tz = useReportingTz();
  const isClient = useIsClient();
  return (
    <p
      className={`text-[11px] text-muted-foreground ${className}`}
      title={t('hint')}
      // 마운트 전에는 빈 줄로 높이만 유지 — 레이아웃이 튀지 않게.
      aria-hidden={!isClient || undefined}
    >
      {isClient ? t(kind, { zone: tzZoneText(tz) }) : ' '}
    </p>
  );
}

/**
 * 브라우저 TZ 로 그린 시간 축(절대 시각 창 차트)의 캡션 — 'Times in GMT+9 (your
 * browser)'. 표는 열 제목에 TZ 를 붙이지만 차트 축에는 제목이 없어 캡션으로 적는다.
 * 마운트 전에는 TZ 를 모르므로 빈 줄.
 */
export function BrowserTzCaption({ className = '' }: { className?: string }) {
  const t = useTranslations('common');
  const tz = useDisplayTz();
  return (
    <p
      className={`text-[11px] text-muted-foreground ${className}`}
      title={tz ? t('viewerTzHint', { tz }) : undefined}
      aria-hidden={!tz || undefined}
    >
      {tz ? t('browserTzCaption', { zone: tzZoneLabel(tz) }) : '\u00a0'}
    </p>
  );
}
