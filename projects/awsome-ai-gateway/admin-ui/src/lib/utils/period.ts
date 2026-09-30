// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AnalyticsFilterForm } from '@/types/api';

/**
 * 리포팅 타임존 — 백엔드와 같은 env (`REPORTING_TIMEZONE`, 기본 Asia/Seoul).
 *
 * 백엔드의 모든 집계는 REPORTING_TIMEZONE 버킷(§59 cost_period_filter /
 * reporting_tz_sql)이다. 배포마다 값이 다를 수 있으므로 **절대 'Asia/Seoul' 을
 * 하드코딩하지 않는다** — helm 의 global.reportingTimezone 이 ConfigMap 을 통해
 * admin-api·scheduler·admin-ui 모두에 같은 값으로 주입된다.
 *
 * ⚠️ 이 env 는 서버 번들에서만 읽힌다. 클라이언트 컴포넌트에서 직접 부르면
 *    undefined → Seoul 로 조용히 떨어지므로, 클라이언트에는 서버 페이지가
 *    tz prop 으로 넘겨야 한다(PeriodSelector/AnalyticsFilter 패턴).
 */
export function reportingTimezone(): string {
  const tz = process.env.REPORTING_TIMEZONE;
  if (!tz) {
    if (typeof window !== 'undefined') {
      console.warn(
        '[period] REPORTING_TIMEZONE is not visible in the client bundle — ' +
          'pass the tz prop from the server page; falling back to Asia/Seoul',
      );
    }
    return 'Asia/Seoul';
  }
  return tz;
}

/**
 * 현재 리포팅 타임존의 연/월/일. 실행 환경 TZ(pod UTC·열람자 로컬)와 무관하다.
 *
 * 예전 헬퍼들은 `new Date().getMonth()` 같은 **로컬 시간**을 썼다. 서버 컴포넌트에서는
 * 그 로컬이 pod 의 UTC 라서 리포팅 TZ 의 새벽 구간에 하루 전 날짜/달을 답했다
 * (일평균 분모 off-by-one, 월초 수 시간 동안 월말 예상이 통째로 사라짐).
 * Intl 로 뽑으면 서버/브라우저 양쪽에서 지정 TZ 기준 동일한 값이 나온다.
 */
export function reportingNowParts(tz = reportingTimezone()): { y: number; m: number; d: number } {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: tz,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).formatToParts(new Date());
  const get = (type: string) => Number(parts.find((p) => p.type === type)!.value);
  return { y: get('year'), m: get('month'), d: get('day') };
}

/** 현재 달력월 'YYYY-MM' (리포팅 TZ 기준 — 백엔드 집계 기준과 일치). */
export function currentCalendarMonth(tz = reportingTimezone()): string {
  const { y, m } = reportingNowParts(tz);
  return `${y}-${String(m).padStart(2, '0')}`;
}

/** N개월 전 달 'YYYY-MM' (0=이번 달, 1=지난 달). 리포팅 TZ 기준. */
export function monthsAgo(n: number, tz = reportingTimezone()): string {
  const { y, m } = reportingNowParts(tz);
  // UTC 산술 — 로컬 TZ 가 개입하지 않는 순수 달력 계산.
  const d = new Date(Date.UTC(y, m - 1 - n, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
}

/** 문자열이 'YYYY-MM' 월 형식인지. */
export function isMonth(value: string | null | undefined): value is string {
  return !!value && /^\d{4}-\d{2}$/.test(value);
}

/** 'YYYY-MM' → 'YYYY년 M월'. 순수 문자열 split (new Date 금지 — TZ drift). */
export function toKoreanMonthLabel(p: string): string {
  const [y, m] = p.split('-');
  return `${y}년 ${Number(m)}월`;
}

/**
 * 절대 월을 상대 레이블로. 이번 달/지난 달이면 그 레이블, 아니면 'YYYY년 M월'.
 * 시간이 흘러도(7월·8월…) 안정적인 표기.
 */
export function relativeMonthLabel(month: string, tz = reportingTimezone()): string {
  if (month === currentCalendarMonth(tz)) return '이번 달';
  if (month === monthsAgo(1, tz)) return '지난 달';
  return toKoreanMonthLabel(month);
}

/**
 * analytics 필터를 백엔드가 기대하는 단일 월(YYYY-MM)로 환산.
 *
 * 백엔드 /admin/analytics 는 reporting TZ 월 버킷으로 단일 월만 필터한다. 필터의
 * 7d/30d/90d 상대 기간은 월 단위로 환산되며, custom 이 아닐 때 "현재 달력월"
 * 대신 latestWithData(데이터 있는 최근 월)를 기본으로 써서 월이 바뀌어도
 * 빈 화면을 피한다.
 *
 * @param filter         analytics 필터 폼
 * @param latestWithData 데이터가 있는 가장 최근 월. 미지정 시 현재 달력월.
 * @param tz             리포팅 타임존 — 클라이언트에서 부를 때 서버에서 넘겨받은 값.
 */
export function resolveMonth(
  filter: AnalyticsFilterForm,
  latestWithData?: string,
  tz = reportingTimezone(),
): string {
  // 명시적 YYYY-MM 월이 흘러들어오면 존중(월 선택기 경로).
  if (isMonth(filter.period as unknown as string)) {
    return filter.period as unknown as string;
  }
  if (filter.period === 'custom' && filter.start_date) {
    return filter.start_date.slice(0, 7);
  }
  return latestWithData ?? currentCalendarMonth(tz);
}
