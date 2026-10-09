'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface MyBudgetResponse {
  user_id: string;
  period: string;
  budget: {
    /** 개인 예산 config 없음(팀 예산/D-cap 적용)이면 null — $0 한도와 구분된다. */
    limit_usd: number | null;
    used_usd: number;
    remaining_usd: number | null;
    usage_pct: number | null;
    /** 개인 예산 없으면 null (기본 HARD_BLOCK 표시는 오해를 만든다). */
    policy: string | null;
  };
}

export interface DailyUsage {
  date: string;
  cost_usd: number;
  requests: number;
  tokens: number;
}

export interface ModelUsage {
  model_alias: string;
  /** 카탈로그 표시명 — modelDisplay(alias, display_name) 재료. 미등록이면 null. */
  display_name?: string | null;
  cost_usd: number;
  requests: number;
  tokens: number;
}

export interface MyUsageResponse {
  user_id: string;
  period: string;
  daily_usage: DailyUsage[];
  by_model: ModelUsage[];
}

export async function fetchMyBudget(): Promise<MyBudgetResponse> {
  return withRetry(() => adminAPI.get<MyBudgetResponse>('/admin/my/budget'));
}

export async function fetchMyUsage(period?: string): Promise<MyUsageResponse> {
  return withRetry(() =>
    adminAPI.get<MyUsageResponse>('/admin/my/usage', period ? { period } : undefined)
  );
}

/** 본인 사용량이 있는 월 목록 — /my 기간 선택기용(dashboard/periods 는 admin 전용). */
export async function fetchMyPeriods(): Promise<string[]> {
  const res = await withRetry(() => adminAPI.get<{ periods: string[] }>('/admin/my/periods'));
  return res.periods ?? [];
}