'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';
import type { WebSearchUsage } from '@/types/entities';

export interface MonitoringOverviewResponse {
  timestamp: string;
  active_models: number;
  last_1h: {
    total_requests: number;
    error_count: number;
    error_rate_pct: number;
    avg_latency_ms: number;
    p95_latency_ms: number;
    /** 모델 비용 — 웹 검색 미포함. */
    total_cost_usd: number;
    /** 모니터링 전용 웹 검색(배포 전 백엔드는 없음). */
    web_search?: WebSearchUsage;
  };
}

export interface MonitoringModelItem {
  alias: string;
  status: string;
  last_1h_requests: number;
  avg_latency_ms: number;
  error_rate_pct: number;
  last_request_at: string | null;
}

export interface MonitoringModelsResponse {
  models: MonitoringModelItem[];
}

export type MonitoringEventTypeFilter =
  | 'all'
  | 'success'
  | 'error'
  | 'timeout'
  | 'cancelled'
  | 'slow'
  | 'abnormal';

export interface MonitoringEvent {
  request_id: string;
  timestamp: string;
  /** agent/service 토큰 호출은 NULL. */
  user_id: string | null;
  user_email?: string;
  model_alias: string;
  downgraded_from?: string | null;
  event_type: string;
  ttft_ms?: number | null;
  latency_ms?: number;
  client?: string | null;
  cost_usd?: number | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  cache_write_tokens?: number | null;
  detail: string;
}

export interface MonitoringEventsResponse {
  events: MonitoringEvent[];
}

export interface MonitoringUserItem {
  user_id: string;
  email: string;
  display_name: string;
  requests: number;
  tokens: number;
  cost_usd: number;
  error_rate_pct: number;
  last_request_at: string | null;
}

export interface MonitoringUsersResponse {
  users: MonitoringUserItem[];
}

export async function fetchMonitoringOverview(): Promise<MonitoringOverviewResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringOverviewResponse>('/admin/monitoring/overview')
  );
}

export async function fetchMonitoringModels(): Promise<MonitoringModelsResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringModelsResponse>('/admin/monitoring/models')
  );
}

export async function fetchMonitoringEvents(
  limit = 50,
  eventType: MonitoringEventTypeFilter = 'all',
  /** 조회 창(일). 생략/1 = 최근 24h, 0 = 기간 제한 없음(본문 로그 뷰어용). */
  days?: number,
): Promise<MonitoringEventsResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringEventsResponse>('/admin/monitoring/events', {
      limit,
      event_type: eventType,
      ...(days !== undefined ? { days } : {}),
    })
  );
}

export async function fetchMonitoringUsers(limit = 10): Promise<MonitoringUsersResponse> {
  return withRetry(() =>
    adminAPI.get<MonitoringUsersResponse>('/admin/monitoring/users', { limit })
  );
}