'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import { normalizeAllocation } from '@/lib/budget-allocation';
import { BudgetSetSchema } from '@/types/api';
import { withRetry } from '@/lib/utils/retry';
import { APIError } from '@/lib/utils/retry';
import type { AllocationEntry, TeamBudgetAllocation } from '@/types/entities';
import type { ActionResult } from './types';

// ─── setBudgetAction ──────────────────────────────────────────────────────────

export async function setBudgetAction(
  formData: unknown,
  confirm = false
): Promise<ActionResult<void>> {
  const parsed = BudgetSetSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  const { target_id, target_type, max_budget_usd, policy, alert_thresholds, default_user_cap_usd } =
    parsed.data;

  try {
    const endpoint =
      target_type === 'TEAM'
        ? `/admin/budgets/team/${target_id}`
        : `/admin/budgets/user/${target_id}`;

    // TEAM: default_user_cap_usd 는 키를 보낼 때만 D 변경 — undefined 면 보존(§3-1).
    const body: Record<string, unknown> = { max_budget_usd, policy, alert_thresholds, confirm };
    if (target_type === 'TEAM' && default_user_cap_usd !== undefined) {
      body.default_user_cap_usd = default_user_cap_usd;
    }
    await withRetry(() => adminAPI.put(endpoint, body));
    revalidatePath('/budgets');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, ...toActionError(err) };
  }
}

// ─── deleteUserBudgetAction ───────────────────────────────────────────────────

export async function deleteUserBudgetAction(
  userId: string,
  confirm = false
): Promise<ActionResult<void>> {
  try {
    await withRetry(() =>
      adminAPI.delete(`/admin/budgets/user/${userId}${confirm ? '?confirm=true' : ''}`)
    );
    revalidatePath('/budgets');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, ...toActionError(err) };
  }
}

// ─── allocateTeamBudgetAction ─────────────────────────────────────────────────

export async function allocateTeamBudgetAction(
  teamId: string,
  allocations: Pick<AllocationEntry, 'target_id' | 'target_type' | 'allocated_usd'>[],
  confirm = false
): Promise<ActionResult<void>> {
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }

  try {
    // 백엔드 AllocateBudgetRequest 는 USER 행만 {user_id, allocated_usd} 로 기대한다.
    // AllocationEntry 는 TEAM 합계 행도 포함하므로 USER 만 골라 필드명을 매핑한다
    // (이전엔 AllocationEntry[] 를 그대로 보내 422 — team 예산 할당 저장이 깨져 있었음).
    const items = allocations
      .filter((e) => e.target_type === 'USER')
      .map((e) => ({ user_id: e.target_id, allocated_usd: e.allocated_usd }));
    await withRetry(() =>
      adminAPI.put(`/admin/budgets/team/${teamId}/allocate`, { allocations: items, confirm })
    );
    revalidatePath('/budgets');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, ...toActionError(err) };
  }
}

// ─── getTeamAllocationAction (D-21: admin 도 임의 팀 조회 가능) ──────────────

export async function getTeamAllocationAction(
  teamId: string
): Promise<ActionResult<TeamBudgetAllocation | null>> {
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }
  try {
    const data = await withRetry(() =>
      adminAPI.get<TeamBudgetAllocation | null>(`/admin/budgets/team/${teamId}/allocation`)
    );
    return { success: true, data: normalizeAllocation(data) };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setTeamDefaultCapAction (D-23: 팀 기본 유저 cap D) ──────────────────────

export async function setTeamDefaultCapAction(
  teamId: string,
  value: number | null,
  confirm = false
): Promise<ActionResult<{ default_user_cap_usd: string | null }>> {
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }
  try {
    const res = await withRetry(() =>
      adminAPI.put<{ default_user_cap_usd: string | null }>(
        `/admin/budgets/team/${teamId}/default-cap`,
        { value, confirm }
      )
    );
    revalidatePath('/budgets');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, ...toActionError(err) };
  }
}

// ─── equalSplitTeamBudgetAction (D-23: D = floor(T/N) 도우미) ────────────────

export async function equalSplitTeamBudgetAction(
  teamId: string,
  clearIndividual: boolean,
  confirm = false
): Promise<ActionResult<{ default_user_cap_usd?: string | null }>> {
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }
  try {
    const res = await withRetry(() =>
      adminAPI.post<{ default_user_cap_usd?: string | null }>(
        `/admin/budgets/team/${teamId}/equal-split`,
        { clear_individual: clearIndividual, confirm }
      )
    );
    revalidatePath('/budgets');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, ...toActionError(err) };
  }
}

// ─── Downgrade Config ────────────────────────────────────────────────────────

import type { AutoDowngradeConfig } from '@/types/entities';

export async function getDowngradeConfigAction(
  scope: string,
  scopeId: string
): Promise<ActionResult<AutoDowngradeConfig>> {
  try {
    const data = await withRetry(() =>
      adminAPI.get<AutoDowngradeConfig>(
        `/admin/budgets/${scope}/${scopeId}/downgrade`
      )
    );
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function setDowngradeConfigAction(
  scope: string,
  scopeId: string,
  config: {
    enabled: boolean;
    rules: { from_model_alias: string; to_model_alias: string; threshold_pct: number }[];
  }
): Promise<ActionResult<AutoDowngradeConfig>> {
  try {
    const data = await withRetry(() =>
      adminAPI.put<AutoDowngradeConfig>(
        `/admin/budgets/${scope}/${scopeId}/downgrade`,
        { enabled: config.enabled, rules: config.rules }
      )
    );
    // 다운그레이드 설정은 요약 테이블 데이터와 무관 — revalidatePath 하면
    // RSC 리페치가 테이블을 리마운트시켜 펼침/패널 상태가 날아간다.
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function deleteDowngradeConfigAction(
  scope: string,
  scopeId: string
): Promise<ActionResult<void>> {
  try {
    await withRetry(() =>
      adminAPI.delete(`/admin/budgets/${scope}/${scopeId}/downgrade`)
    );
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** 409 confirmation_required 를 구조화된 confirmation 으로, 나머지는 error 문자열로. */
function toActionError(err: unknown): {
  error: string;
  confirmation?: { message: string; details?: unknown };
} {
  if (err instanceof APIError && err.status === 409 && err.error_code === 'confirmation_required') {
    return { error: err.message, confirmation: { message: err.message, details: err.details } };
  }
  return { error: toErrorMessage(err) };
}

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