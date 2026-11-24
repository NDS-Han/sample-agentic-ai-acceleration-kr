'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 웹 검색 통제 서버 액션 — /admin/web-search-quotas (budget.web_search_policies).
 * 검색 수 기준, 예산과 별개. 쓰기는 사람 관리자 전용이고 감사 로그(SET_WEB_SEARCH_QUOTA /
 * SET_WEB_SEARCH_REQUEST_CAP / CLEAR_WEB_SEARCH_QUOTA)를 남긴다 — 재시도하지 않는다.
 */

import { revalidatePath } from 'next/cache';
import { z } from 'zod';

import { adminAPI } from '@/lib/api-client';
import { APIError, withRetry } from '@/lib/utils/retry';
import type { WebSearchPolicyValues, WebSearchQuotaOverview } from '@/types/entities';

import type { ActionResult } from './types';

const Scope = z.enum(['org', 'teams', 'users']);
const Id = z.string().uuid();
const Values = z.object({
  monthly_query_limit: z.number().int().min(0).max(10_000_000).nullable(),
  per_request_cap: z.number().int().min(1).max(50).nullable(),
});

function path(scope: z.infer<typeof Scope>, id?: string): string {
  return scope === 'org' ? '/admin/web-search-quotas/org' : `/admin/web-search-quotas/${scope}/${id}`;
}

export async function fetchWebSearchQuotaOverview(): Promise<ActionResult<WebSearchQuotaOverview>> {
  try {
    const data = await withRetry(() => adminAPI.get<WebSearchQuotaOverview>('/admin/web-search-quotas'));
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function setWebSearchQuotaAction(
  scope: 'org' | 'teams' | 'users',
  id: string | null,
  values: WebSearchPolicyValues,
): Promise<ActionResult<WebSearchPolicyValues>> {
  try {
    const s = Scope.parse(scope);
    const target = s === 'org' ? undefined : Id.parse(id);
    const body = Values.parse(values);
    const data = await adminAPI.put<WebSearchPolicyValues>(path(s, target), body);
    revalidatePath('/budgets');
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function clearWebSearchQuotaAction(
  scope: 'org' | 'teams' | 'users',
  id: string | null,
): Promise<ActionResult<{ cleared: boolean }>> {
  try {
    const s = Scope.parse(scope);
    const target = s === 'org' ? undefined : Id.parse(id);
    const data = await adminAPI.delete<{ cleared: boolean }>(path(s, target));
    revalidatePath('/budgets');
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

function toErrorMessage(err: unknown): string {
  if (err instanceof APIError) return err.message;
  if (err instanceof z.ZodError) return err.issues[0]?.message ?? 'Validation error';
  if (err instanceof Error) return err.message;
  return 'An unexpected error occurred';
}
