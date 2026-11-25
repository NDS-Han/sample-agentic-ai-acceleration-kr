// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 대시보드 COST & BUDGET 줄 계산 — Total (charged) = 모델 비용(성공+실패+취소) + 웹 검색 비용.
 *
 * 백엔드가 charged 를 주면(wave 3) 그 값을 쓴다(청구 대상 정의는 서버 한 곳 — core/usage_filters).
 * 이 모듈은 예산 값을 읽지도 바꾸지도 않는다 — 예산 사용률은 백엔드 값 그대로.
 */

import type { DashboardKPI } from '@/lib/actions/dashboard';
import { reportingNowParts } from '@/lib/utils/period';
import { webSearchVisible } from '@/lib/utils/webSearchVisibility';

/**
 * 일 평균 소비 + (당월이면) 월말 예상.
 * - 당월: 오늘까지 경과일로 나눠 일평균 → 그 달 총일수 곱해 월말 예상(선형 추정).
 * - 과거월: 그 달 총일수로 나눔(예상 없음, 이미 확정).
 * ⚠️ "지금" 은 리포팅 TZ — 분자가 REPORTING_TIMEZONE 버킷으로 집계되므로 분모도 같은 달력.
 */
export function computeDailyAvg(
  period: string,
  totalCost: number,
  now: { y: number; m: number; d: number } = reportingNowParts(),
): { dailyAvg: number; projection: number | null } {
  const [y, m] = period.split('-').map(Number);
  const daysInMonth = new Date(Date.UTC(y, m, 0)).getUTCDate();
  const isCurrentMonth = y === now.y && m === now.m;
  const elapsedDays = isCurrentMonth ? now.d : daysInMonth;
  const dailyAvg = elapsedDays > 0 ? totalCost / elapsedDays : 0;
  const projection = isCurrentMonth ? dailyAvg * daysInMonth : null;
  return { dailyAvg, projection };
}

export interface CostBudgetView {
  /** 웹 검색을 합쳐 보이는가(표시 규칙). false 면 카드가 기능 이전과 똑같다. */
  showWebSearch: boolean;
  modelCost: number;
  /** 기록된 웹 검색 비용(미추적 null 은 0 으로 더한다 — 합계 표시용). */
  webSearchCost: number;
  /** 그 기간 웹 검색 비용이 기록됐는가 — false(NULL)면 합계에 웹 검색이 빠져 있으므로
      부제는 "$0.00" 이 아니라 "Web search not tracked" 라고 말한다. */
  webSearchTracked: boolean;
  /** 'Usage This Month' 값 — 모델 + 웹 검색(표시 규칙이 거짓이면 모델만). */
  usageCost: number;
  avgPerUser: number;
  dailyAvg: number;
  projection: number | null;
  /** 예산 사용률 — **항상 모델 비용 기준**(백엔드 값 그대로). */
  budgetUtilization: number | null;
}

export function costBudgetView(
  kpi: DashboardKPI,
  period: string,
  now?: { y: number; m: number; d: number },
): CostBudgetView {
  const showWebSearch = webSearchVisible(kpi.web_search);
  const modelCost = kpi.charged?.model_usd ?? kpi.total_cost_usd;
  const webSearchCost = showWebSearch ? (kpi.web_search?.cost_usd ?? 0) : 0;
  const webSearchTracked = kpi.web_search?.cost_usd != null;
  // 웹 검색은 청구 총액의 일부(DECISION 2) — 표시 규칙과 무관하게 기록된 비용이 있으면 총액에 든다.
  const usageCost = kpi.charged ? kpi.charged.total_usd : modelCost + webSearchCost;
  const avgPerUser = showWebSearch
    ? kpi.active_users > 0
      ? usageCost / kpi.active_users
      : 0
    : kpi.cost_per_user_usd;
  const { dailyAvg, projection } = computeDailyAvg(period, usageCost, now);
  return {
    showWebSearch,
    modelCost,
    webSearchCost,
    webSearchTracked,
    usageCost,
    avgPerUser,
    dailyAvg,
    projection,
    budgetUtilization: kpi.budget_utilization_pct,
  };
}
