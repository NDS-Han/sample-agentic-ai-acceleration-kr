'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import { withRetry, APIError } from '@/lib/utils/retry';

export interface BodyLogRecord {
  request_id: string;
  provider: string;
  client: string;
  model_alias: string;
  status: 'success' | 'error' | 'partial';
  is_streaming: boolean;
  user_id?: string | null;
  team_id?: string | null;
  sso_subject?: string | null;
  bedrock_request_id?: string | null;
  request_body: Record<string, unknown>;
  response_body: Record<string, unknown>;
  error?: Record<string, unknown> | null;
  requested_at?: string;
  completed_at?: string;
  schema_version?: number;
}

export interface BodyLogResponse {
  found: boolean;
  request_id: string;
  source: 'firehose' | 'fallback' | null;
  key: string | null;
  duplicates: number;
  oversized: boolean;
  /** 스캔 상한/파손으로 일부 구간을 못 봤으면 true — 레코드는 정상 반환된다. */
  incomplete?: boolean;
  model_alias: string;
  /** usage row 의 denormalized 이메일 — 레코드의 user_id 보다 읽기 쉬운 표시용. */
  user_email?: string | null;
  /** usage row 의 요청 시각 — 레코드 자체 시각이 아니라 진짜 호출 시각. */
  requested_at: string;
  record: BodyLogRecord | null;
}

export interface BodyLogResult {
  success: boolean;
  data?: BodyLogResponse;
  error?: string;
  status?: number;
}

/** data 페이로드만 다른 동일 계약 — sessions/batch 응답용. */
export interface BodyLogResultT<T> {
  success: boolean;
  data?: T;
  error?: string;
  status?: number;
}

export async function fetchBodyLog(requestId: string): Promise<BodyLogResult> {
  try {
    const data = await withRetry(() =>
      adminAPI.get<BodyLogResponse>(
        `/admin/body-logs/${encodeURIComponent(requestId)}`,
      ),
    );
    return { success: true, data };
  } catch (err) {
    // APIError.message 에 FastAPI detail 이 이미 포맷돼 들어있다 — status 만 따로 넘긴다.
    return {
      success: false,
      error: err instanceof Error ? err.message : String(err),
      status: err instanceof APIError ? err.status : undefined,
    };
  }
}

// ── 세션 trace (Langfuse 스타일 — 대화 단위 묶음) ─────────────────────────────

export interface BodyLogSessionCall {
  request_id: string;
  requested_at: string;
  model_alias: string | null;
  status: string | null;
  latency_ms: number | null;
  cost_usd: number | null;
}

export interface BodyLogSession {
  key: string;
  /** 명시적 세션 키 (usage_logs.session_id). NULL=시간 인접성 휴리스틱 그룹. */
  session_id: string | null;
  user_id: string | null;
  user_email: string | null;
  client: string | null;
  started_at: string;
  ended_at: string;
  call_count: number;
  error_count: number;
  models: string[];
  calls: BodyLogSessionCall[];
}

export interface BodyLogSessionsResponse {
  sessions: BodyLogSession[];
  scanned_rows: number;
  /** 스캔 상한에 걸림 — 더 오래된 세션이 있을 수 있다. */
  truncated: boolean;
  /** 어떤 세션이든 calls 가 세션당 상한에 잘렸다. */
  calls_truncated: boolean;
}

export async function fetchBodyLogSessions(
  days = 1,
): Promise<BodyLogResultT<BodyLogSessionsResponse>> {
  try {
    const data = await withRetry(() =>
      adminAPI.get<BodyLogSessionsResponse>(
        `/admin/body-logs/sessions?days=${days}`,
      ),
    );
    return { success: true, data };
  } catch (err) {
    return {
      success: false,
      error: err instanceof Error ? err.message : String(err),
      status: err instanceof APIError ? err.status : undefined,
    };
  }
}

export interface BodyLogBatchItem {
  found: boolean;
  source: 'firehose' | 'fallback' | null;
  key: string | null;
  duplicates: number;
  record: BodyLogRecord | null;
  model_alias?: string;
  user_email?: string | null;
  requested_at?: string;
}

export interface BodyLogBatchResponse {
  records: Record<string, BodyLogBatchItem>;
  oversized: string[];
  missing: string[];
  incomplete: boolean;
}

export async function fetchBodyLogBatch(
  requestIds: string[],
): Promise<BodyLogResultT<BodyLogBatchResponse>> {
  try {
    const data = await withRetry(() =>
      adminAPI.post<BodyLogBatchResponse>('/admin/body-logs/batch', {
        request_ids: requestIds,
      }),
    );
    return { success: true, data };
  } catch (err) {
    return {
      success: false,
      error: err instanceof Error ? err.message : String(err),
      status: err instanceof APIError ? err.status : undefined,
    };
  }
}
