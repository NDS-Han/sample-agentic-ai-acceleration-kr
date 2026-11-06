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

/** usage 행에서 가져오는 호출 메타 — 본문 레코드에 없는 과금 정보. */
export interface BodyLogUsageMeta {
  cost_usd?: number | null;
  latency_ms?: number | null;
  ttft_ms?: number | null;
  client?: string | null;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cache_read_tokens?: number | null;
  reasoning_tokens?: number | null;
}

export interface BodyLogResponse extends BodyLogUsageMeta {
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

// 503 은 "버킷 미설정" 같은 결정적 실패고, 어떤 재시도든 VIEW_BODY_LOG 감사
// 행과 S3 스캔을 한 번 더 만든다 — 네트워크 오류만 재시도한다.
const NO_HTTP_RETRY = { retryOn: [] as number[] };

export async function fetchBodyLog(requestId: string): Promise<BodyLogResult> {
  try {
    const data = await withRetry(
      () =>
        adminAPI.get<BodyLogResponse>(
          `/admin/body-logs/${encodeURIComponent(requestId)}`,
        ),
      NO_HTTP_RETRY,
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
  input_tokens: number | null;
  output_tokens: number | null;
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
  total_cost_usd: number | null;
  models: string[];
  calls: BodyLogSessionCall[];
}

export interface BodyLogSessionsResponse {
  sessions: BodyLogSession[];
  /** 휴리스틱 경로에서 스캔한 NULL session_id 행 수. */
  scanned_rows: number;
  /** 집계된 명시 세션 수 (limit+1 조회라 limit 까지 잘린 값). */
  explicit_sessions: number;
  /** NULL 행 스캔 상한에 걸림 — 더 오래된 세션이 있을 수 있다. */
  truncated: boolean;
  /** 어떤 세션이든 calls 가 세션당 상한에 잘렸다. */
  calls_truncated: boolean;
  /** user 필터 부분일치가 100명 상한에 걸려 일부 사용자의 세션이 빠졌다. */
  user_filter_truncated?: boolean;
  has_more: boolean;
  /** 다음 페이지 커서 — 직전 페이지 마지막 세션의 ended_at. */
  next_before: string | null;
}

export interface BodyLogSessionFilters {
  days?: number;
  /** 커서 — 이 시각 이전에 끝난 세션만. */
  before?: string;
  /** before 와 같은 ended_at 으로 이미 받은 세션 key — 타이 배제용. */
  beforeKeys?: string[];
  /** 이메일/user_id 부분일치. */
  user?: string;
  client?: string;
  hasError?: boolean;
  /** 명시 session_id 직행 조회 (딥링크) — 목록 스캔 생략. */
  sid?: string;
  limit?: number;
}

export async function fetchBodyLogSessions(
  f: BodyLogSessionFilters = {},
): Promise<BodyLogResultT<BodyLogSessionsResponse>> {
  try {
    const qs = new URLSearchParams();
    qs.set('days', String(f.days ?? 1));
    if (f.before) qs.set('before', f.before);
    for (const k of f.beforeKeys ?? []) qs.append('before_key', k);
    if (f.user) qs.set('user', f.user);
    if (f.client) qs.set('client', f.client);
    if (f.hasError) qs.set('has_error', 'true');
    if (f.sid) qs.set('sid', f.sid);
    if (f.limit) qs.set('limit', String(f.limit));
    const data = await withRetry(
      () => adminAPI.get<BodyLogSessionsResponse>(`/admin/body-logs/sessions?${qs}`),
      NO_HTTP_RETRY,
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

export interface BodyLogBatchItem extends BodyLogUsageMeta {
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
    const data = await withRetry(
      () =>
        adminAPI.post<BodyLogBatchResponse>('/admin/body-logs/batch', {
          request_ids: requestIds,
        }),
      NO_HTTP_RETRY,
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
