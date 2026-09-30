'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { createContext, useContext } from 'react';

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
