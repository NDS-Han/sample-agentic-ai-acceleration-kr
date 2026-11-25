'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useMemo } from 'react';
import { useLocale, useTranslations } from 'next-intl';

import { useDisplayTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateMinute, fmtDateTime, fmtHourMinute, fmtTime } from '@/lib/utils/format';
import { tzAbbr, tzExact, tzZoneLabel, withTzAbbr } from '@/lib/utils/timezone';

/**
 * 한 시점(timestamp) 포맷터 — 브라우저 TZ 로 그리고 TZ 이름을 붙인다.
 *
 * 마운트 전(SSR·하이드레이션)에는 tz=null 이라 모든 포맷 결과가 '' 다 — 파드 TZ 로
 * 그린 시각이 잠깐 보이거나 하이드레이션 불일치가 생기지 않는다.
 *  - dateTimeZ/timeZ: 셀마다 TZ 이름을 붙인 문자열('11/06, 09:13:22 GMT+9').
 *  - dateTime/time + header(): 빽빽한 표는 열 제목에 한 번만('Time (GMT+9)').
 *    열 제목 이름은 DST 독립(tzZoneLabel: 'PT'·'Europe/Berlin') — 'now' 의 약어(PST)를
 *    쓰면 DST 전환을 낀 표에서 옛 행(PDT)과 어긋난다. 행별 정확한 약어·오프셋은
 *    cellTitle(iso) 툴팁('PDT (GMT-7)')으로 셀마다 준다.
 *  - stamp/stampZ: 연도 포함 분 단위('2026-11-06 09:13') — 발급일·마지막 요청 등.
 *  - hmZ: 시·분만 + TZ 이름('14:05 GMT+9') — 집계 기준 시각(DataFreshness).
 */
export function usePointInTime() {
  const locale = useLocale();
  const tc = useTranslations('common');
  const tz = useDisplayTz();
  return useMemo(() => {
    // DST 독립 이름 — 열 제목·여러 시점을 덮는 라벨용.
    const abbr = tz ? tzZoneLabel(tz) : '';
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
      stamp: (iso: string) => (tz ? fmtDateMinute(iso, tz) : ''),
      stampZ: (iso: string) => (tz ? withTzAbbr(fmtDateMinute(iso, tz), tz, iso) : ''),
      timeZ: (iso: string) => (tz ? withTzAbbr(fmtTime(iso, locale, tz), tz, iso) : ''),
      /** 'HH:MM GMT+9' — 시·분 + 그 시점 TZ 이름(집계 기준 시각 등). */
      hmZ: (iso: string) => (tz ? withTzAbbr(fmtHourMinute(iso, tz), tz, iso) : ''),
      /** 열 제목 + TZ — 마운트 전에는 라벨만. */
      header: (label: string) => (abbr ? tc('withZone', { label, zone: abbr }) : label),
      /** 셀 툴팁 — 어느 시계인지 IANA 이름까지. */
      title: tz ? tc('viewerTzHint', { tz }) : undefined,
      /** 그 시점의 약어('PDT', 'IST', 'GMT+9') — 한 시점 옆에 붙일 때. */
      zoneAt: (iso: string) => (tz ? tzAbbr(tz, new Date(iso)) : ''),
      /** 행별 셀 툴팁 — 'PDT (GMT-7), your browser time zone (America/Los_Angeles)'. */
      cellTitle: (iso: string | null | undefined) =>
        tz && iso ? tc('viewerTzCellHint', { zone: tzExact(tz, new Date(iso)), tz }) : undefined,
    };
  }, [tz, locale, tc]);
}
