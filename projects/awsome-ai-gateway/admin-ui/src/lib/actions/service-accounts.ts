'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { revalidatePath } from 'next/cache';
import { adminAPI } from '@/lib/api-client';
import { APIError } from '@/lib/utils/retry';
import type {
  IssueServiceKeyResponse,
  ServiceAccountListResponse,
} from '@/types/entities';
import type { ActionResult } from './types';

function toErrorMessage(err: unknown): string {
  if (err instanceof APIError) return err.message;
  if (err instanceof Error) return err.message;
  return 'An unexpected error occurred';
}

// ─── listServiceAccountsAction ────────────────────────────────────────────────

export async function listServiceAccountsAction(): Promise<
  ActionResult<ServiceAccountListResponse>
> {
  try {
    const data = await adminAPI.get<ServiceAccountListResponse>(
      '/admin/service-accounts',
    );
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}


// ─── unmarkServiceAccountAction ───────────────────────────────────────────────

export async function unmarkServiceAccountAction(
  userId: string,
): Promise<ActionResult<void>> {
  try {
    await adminAPI.delete(`/admin/service-accounts/${encodeURIComponent(userId)}`);
    revalidatePath('/keys');
    revalidatePath('/agents');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── issueServiceKeyAction ────────────────────────────────────────────────────
// 쓰기는 재시도하지 않는다(revoke 와 같은 이유) — 유저당 ACTIVE 1개라
// 재시도가 기존 키를 또 만료시킨다.
// 표지+발급은 한 트랜잭션(POST /admin/service-accounts/{id}/issue) — 분리하면
// 표지만 commit 되고 발급이 실패할 때 사람 계정이 로그인 불가 상태로 갇힌다.

export async function issueServiceKeyAction(params: {
  userId: string;
  ttlDays?: number;
}): Promise<ActionResult<IssueServiceKeyResponse>> {
  const body: Record<string, unknown> = {};
  if (params.ttlDays !== undefined) body.ttl_days = params.ttlDays;
  try {
    const data = await adminAPI.post<IssueServiceKeyResponse>(
      `/admin/service-accounts/${encodeURIComponent(params.userId)}/issue`,
      body,
    );
    revalidatePath('/keys');
    revalidatePath('/agents');
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── listIssueTargetsAction ───────────────────────────────────────────────────
// 발급 모달 드롭다운용 — 활성 비ADMIN 유저 전체. 미플래그 선택지는 발급 시
// 서비스 계정으로 자동 전환된다(플래그 경로가 모달에 통합).

export interface IssueTargetUser {
  id: string;
  email: string;
  display_name: string;
  role: string;
  team_name: string | null;
  is_service_account: boolean;
}

export async function listIssueTargetsAction(): Promise<
  ActionResult<{ items: IssueTargetUser[] }>
> {
  try {
    const data = await adminAPI.get<{
      items: Array<{
        id: string;
        email: string;
        display_name: string;
        role: string;
        team_name: string | null;
        is_service_account?: boolean;
      }>;
    }>('/admin/users', { is_active: 'true', limit: 200 });
    return {
      success: true,
      data: {
        items: (data.items ?? [])
          .filter((u) => u.role !== 'ADMIN')
          .map((u) => ({ ...u, is_service_account: u.is_service_account ?? false })),
      },
    };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── repairServiceAccountAction ───────────────────────────────────────────────

export async function repairServiceAccountAction(
  userId: string,
): Promise<ActionResult<{ repaired: number }>> {
  try {
    const data = await adminAPI.post<{ repaired: number }>(
      `/admin/service-accounts/${encodeURIComponent(userId)}/repair`,
      {},
    );
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}
