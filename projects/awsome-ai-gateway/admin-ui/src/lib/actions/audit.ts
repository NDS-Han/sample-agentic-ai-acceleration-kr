'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import { withRetry, APIError } from '@/lib/utils/retry';
import type { AuditLogListResponse } from '@/types/entities';
import type { ActionResult } from './types';

export interface AuditLogQuery {
  cursor: string | null;
  range: string;
  group?: string;
  action?: string;
  actor_email?: string;
  resource_type?: string;
  resource_id?: string;
  limit: number;
}

/**
 * 감사 로그 목록 — "더 보기" 전용. 첫 페이지는 서버 컴포넌트가 직접 가져오고
 * 이후 페이지만 이 액션 경유 (listKeysAction 패턴).
 */
export async function listAuditLogsAction(
  params: AuditLogQuery,
): Promise<ActionResult<AuditLogListResponse>> {
  const query: Record<string, string | number> = {
    range: params.range,
    limit: params.limit,
  };
  if (params.cursor) query.cursor = params.cursor;
  if (params.group) query.group = params.group;
  if (params.action) query.action = params.action;
  if (params.actor_email) query.actor_email = params.actor_email;
  if (params.resource_type) query.resource_type = params.resource_type;
  if (params.resource_id) query.resource_id = params.resource_id;
  try {
    const data = await withRetry(() =>
      adminAPI.get<AuditLogListResponse>('/admin/audit/logs', query),
    );
    return { success: true, data };
  } catch (err) {
    return {
      success: false,
      error: err instanceof APIError ? err.message : 'Unexpected error',
    };
  }
}
