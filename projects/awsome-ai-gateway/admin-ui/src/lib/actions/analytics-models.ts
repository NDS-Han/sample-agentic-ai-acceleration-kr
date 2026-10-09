'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';

export interface ModelCostItem {
  model_alias: string;
  /** 카탈로그 표시명 — modelDisplay(alias, display_name)의 재료. 미등록이면 null. */
  display_name: string | null;
  request_count: number;
  total_cost_usd: number;
  input_tokens: number;
  output_tokens: number;
  /** 별도 과금 버킷 — input/output 에 미포함. 실효 단가 분모에는 포함. */
  cache_read_tokens: number;
  cache_creation_tokens: number;
  avg_latency_ms: number;
  cost_per_1k_tokens: number;
}

export interface DailyModelCost {
  date: string;
  model_alias: string;
  cost_usd: number;
}

export interface ModelCostAnalyticsResponse {
  period: string;
  total_cost_usd: number;
  models: ModelCostItem[];
  daily_trend: DailyModelCost[];
}

export interface ModelCostFilter {
  /** YYYY-MM — 생략 시 백엔드가 KST 현재 월. */
  period?: string;
  /** custom 구간 — 둘 다 있을 때만 의미 있다(백엔드는 한쪽만 오면 400). */
  start_date?: string | null;
  end_date?: string | null;
  /** 'all' | 'team:{uuid}' */
  scope?: string | null;
  client?: string | null;
}

// overview(/admin/analytics)와 같은 필터를 그대로 전달 — 같은 화면의 카드와
// 표가 같은 집합을 보게 하기 위함. 섹션 통합 후에도 단일 소스는 이 함수.
export async function fetchModelCostAnalytics(
  filter: ModelCostFilter = {}
): Promise<ModelCostAnalyticsResponse> {
  const params: Record<string, string> = {};
  if (filter.period) params.period = filter.period;
  if (filter.start_date && filter.end_date) {
    params.start_date = filter.start_date;
    params.end_date = filter.end_date;
  }
  if (filter.scope) params.scope = filter.scope;
  if (filter.client && filter.client !== 'all') params.client = filter.client;
  return withRetry(() =>
    adminAPI.get<ModelCostAnalyticsResponse>(
      '/admin/analytics/models',
      Object.keys(params).length ? params : undefined
    )
  );
}