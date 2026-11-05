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
