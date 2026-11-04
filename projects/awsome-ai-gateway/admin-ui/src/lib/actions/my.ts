'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface MyBudgetResponse {
  user_id: string;
  period: string;
  /** 팀 소속이면 팀 공용 풀 상태 — 개인 한도와 별개로 소진 시 요청이 차단된다.
   *  팀이 없으면 null. */
  team: {
    team_id: string;
    team_name: string | null;
    /** T — 미설정(config 없음/NULL)이면 null, 그 상태는 차단(429)이다. */
    limit_usd: number | null;
    used_usd: number;
    remaining_usd: number | null;
    usage_pct: number | null;
    /** 팀 기본 개인 상한(D) — 개인 A_u 가 없을 때 본인에게 적용되는 cap. */
    default_cap_usd: number | null;
  } | null;
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
  /** 모델별 토큰 버킷 — summary 와 같은 정의(§59 SUCCESS + KST). */
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
}

export interface MyUsageSummary {
  requests: number;
  /** ERROR+TIMEOUT — 전체 요청 = requests + error_requests */
  error_requests: number;
  cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  cache_creation_tokens: number;
  cache_read_tokens: number;
  /** output 에 이미 포함된 비청구 표시 지표 — total 에 더하면 이중계산. */
  reasoning_tokens: number;
  web_search_count: number;
}

export interface MyUsageResponse {
  user_id: string;
  period: string;
  summary: MyUsageSummary;
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