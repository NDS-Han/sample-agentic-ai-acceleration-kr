// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 예산 경보 임계값의 단일 출처 — admin-api 의 _alert_level 서비스 로직과 동일
 * (>=90 CRITICAL / >=70 WARNING). 'use client' 가 붙은 컴포넌트 파일에서
 * import하면 서버 컴포넌트(budgets/page.tsx, 대시보드 page.tsx)가 쓸 수 없어
 * 순수 lib 으로 둔다.
 */

import { AlertLevel } from '@/types/enums';

export type AlertLevelValue = (typeof AlertLevel)[keyof typeof AlertLevel];

export function alertLevelOf(pct: number | null): AlertLevelValue {
  if (pct == null) return AlertLevel.NORMAL;
  if (pct >= 90) return AlertLevel.CRITICAL;
  if (pct >= 70) return AlertLevel.WARNING;
  return AlertLevel.NORMAL;
}
