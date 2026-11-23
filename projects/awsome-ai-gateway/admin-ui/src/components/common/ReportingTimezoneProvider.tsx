'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { createContext, useContext, useSyncExternalStore } from 'react';

const ReportingTzContext = createContext<string>('Asia/Seoul');

/**
 * 리포팅 타임존을 클라이언트 트리에 주입한다.
 *
 * `REPORTING_TIMEZONE` env 는 서버 번들에서만 읽히므로, 클라이언트 컴포넌트가
 * 달력월 경계("이번 달" 버튼, export 파일명 날짜)를 계산하려면 root layout(서버)이
 * env 를 읽어 이 provider 로 넘겨야 한다. 값은 백엔드 집계와 같은
 * global.reportingTimezone 으로 helm 이 주입해 서비스 간 불일치가 불가능하다.
 */
export function ReportingTimezoneProvider({
  tz,
  children,
}: {
  tz: string;
  children: React.ReactNode;
}) {
  return <ReportingTzContext.Provider value={tz}>{children}</ReportingTzContext.Provider>;
}

/** 클라이언트 컴포넌트용 — 서버 env 와 동일한 리포팅 타임존 문자열. */
export function useReportingTz(): string {
  return useContext(ReportingTzContext);
}

const noopSubscribe = () => () => {};

/**
 * 시점(timestamp) 표시용 타임존 — 브라우저 TZ.
 *
 * 리포팅 TZ 는 집계 버킷(일/월 경계)용이다. "기준 시각", 이벤트·감사 로그 시각 같은
 * 한 시점은 보는 사람의 시계로 읽혀야 한다 — dev 처럼 리포팅 TZ 가 Asia/Kolkata 면
 * KST 운영자에게 3시간 30분 어긋난 시각이 보였다.
 * SSR/하이드레이션 중에는 리포팅 TZ 로 그리고(서버·클라 일치), 마운트 직후 브라우저 TZ 로 다시 그린다.
 */
export function useDisplayTz(): string {
  const reportingTz = useReportingTz();
  return useSyncExternalStore(noopSubscribe, browserTz, () => reportingTz) || reportingTz;
}

function browserTz(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone;
  } catch {
    return '';
  }
}
