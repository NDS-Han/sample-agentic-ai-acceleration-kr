'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useMemo } from 'react';
import { useLocale, useTranslations } from 'next-intl';

import { useDisplayTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime, fmtTime } from '@/lib/utils/format';
import { tzAbbr, withTzAbbr } from '@/lib/utils/timezone';

/**
 * 한 시점(timestamp) 포맷터 — 브라우저 TZ 로 그리고 TZ 이름을 붙인다.
 *
 * 마운트 전(SSR·하이드레이션)에는 tz=null 이라 모든 포맷 결과가 '' 다 — 파드 TZ 로
 * 그린 시각이 잠깐 보이거나 하이드레이션 불일치가 생기지 않는다.
 *  - dateTimeZ/timeZ: 셀마다 TZ 이름을 붙인 문자열('11/06, 09:13:22 GMT+9').
 *  - dateTime/time + header(): 빽빽한 표는 열 제목에 한 번만('Time (GMT+9)').
 */
export function usePointInTime() {
  const locale = useLocale();
  const tc = useTranslations('common');
  const tz = useDisplayTz();
  return useMemo(() => {
    const abbr = tz ? tzAbbr(tz) : '';
    const dateTime = (iso: string, loc: string = locale) =>
      tz ? fmtDateTime(iso, loc, tz) : '';
    const time = (iso: string) => (tz ? fmtTime(iso, locale, tz) : '');
    return {
      tz,
      abbr,
      dateTime,
      time,
      dateTimeZ: (iso: string, loc: string = locale) =>
        tz ? withTzAbbr(fmtDateTime(iso, loc, tz), tz, iso) : '',
      timeZ: (iso: string) => (tz ? withTzAbbr(fmtTime(iso, locale, tz), tz, iso) : ''),
      /** 열 제목 + TZ — 마운트 전에는 라벨만. */
      header: (label: string) => (abbr ? tc('withZone', { label, zone: abbr }) : label),
      /** 셀 툴팁 — 어느 시계인지 IANA 이름까지. */
      title: tz ? tc('viewerTzHint', { tz }) : undefined,
    };
  }, [tz, locale, tc]);
}
