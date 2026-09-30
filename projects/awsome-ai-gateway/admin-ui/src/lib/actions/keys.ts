'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';
import { APIError } from '@/lib/utils/retry';
import type { VirtualKeyListItem } from '@/types/entities';
import type { ActionResult } from './types';

// ─── listKeysAction ───────────────────────────────────────────────────────────

export interface CursorPaginationMeta {
  cursor: string | null;
  limit: number;
  has_more: boolean;
}

export interface KeyListResponse {
  items: VirtualKeyListItem[];
  pagination: CursorPaginationMeta;
}

/**
 * 커서 기반 목록 조회 — /keys 페이지의 "더 보기" 가 쓴다.
 * 초기 페이지는 서버 컴포넌트가 직접 adminAPI.get 하고, 이후 페이지만 이 액션 경유.
 */
export async function listKeysAction(params: {
  cursor: string | null;
  email?: string;
  status: string;
  limit: number;
}): Promise<ActionResult<KeyListResponse>> {
  const query: Record<string, string | number> = { limit: params.limit };
  if (params.cursor) query.cursor = params.cursor;
  if (params.email) query.email = params.email;
  query.status = params.status;
  try {
    const data = await withRetry(() => adminAPI.get<KeyListResponse>('/admin/keys', query));
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── revokeKeyAction ──────────────────────────────────────────────────────────

export async function revokeKeyAction(keyId: string): Promise<ActionResult<void>> {
  if (!keyId) {
    return { success: false, error: 'Key ID is required' };
  }

  try {
    await withRetry(() => adminAPI.delete(`/admin/keys/${keyId}`));
    revalidatePath('/keys');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function toErrorMessage(err: unknown): string {
  if (err instanceof APIError) {
    return err.message;
  }
  if (err instanceof z.ZodError) {
    return err.issues[0]?.message ?? 'Validation error';
  }
  if (err instanceof Error) {
    return err.message;
  }
  return 'An unexpected error occurred';
}