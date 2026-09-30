'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


// NOTE: 조직 구조(부서/팀 생성, 사용자 팀 배정)는 Cognito 그룹을 원천으로
// 동기화되며, 관련 서버 액션과 UI 는 제거되었습니다.
// 단 TEAM_LEADER 는 Cognito 그룹이 아니라 이 admin UI("팀 리더 지정")에서만
// 부여됩니다 — sync/재로그인이 DEVELOPER 로 되돌리지 않도록 백엔드가 보존합니다
// (admin-api cognito_sync_service._effective_role / oidc_service.py).

import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import { withRetry } from '@/lib/utils/retry';
import { APIError } from '@/lib/utils/retry';
import type { UserSearchItem } from '@/types/entities';
import type { ActionResult } from './types';

// ─── setTeamLeaderAction ──────────────────────────────────────────────────────

export async function setTeamLeaderAction(
  userId: string,
  teamId: string
): Promise<ActionResult<void>> {
  if (!userId) {
    return { success: false, error: 'User ID is required' };
  }
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }

  try {
    await withRetry(() =>
      adminAPI.put(`/admin/teams/${teamId}/leader`, { user_id: userId })
    );
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── unsetTeamLeaderAction ────────────────────────────────────────────────────

export async function unsetTeamLeaderAction(
  teamId: string,
  userId: string
): Promise<ActionResult<void>> {
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }
  if (!userId) {
    return { success: false, error: 'User ID is required' };
  }

  try {
    await withRetry(() => adminAPI.delete(`/admin/teams/${teamId}/leaders/${userId}`));
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── forceReauthTeamAction ────────────────────────────────────────────────────
// 팀 멤버 전원의 ACTIVE VK 일괄 revoke. 오프보딩/보안 사고/즉시 정책 반영 용도.
// 사용자는 다음 호출 시 401 → Claude Code 재실행 필요 (UI 에서 명시).

export async function forceReauthTeamAction(
  teamId: string
): Promise<ActionResult<{ revoked_count: number }>> {
  if (!teamId) {
    return { success: false, error: 'Team ID is required' };
  }
  try {
    const res = await withRetry(() =>
      adminAPI.post<{ revoked_count: number }>(
        `/admin/teams/${teamId}/force-reauth`,
        {}
      )
    );
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── syncCognitoAction ─────────────────────────────────────────────────────
// Cognito User Pool 에서 그룹/사용자를 가져와 DB 동기화.

export async function syncCognitoAction(): Promise<
  ActionResult<{
    groups_synced: number;
    users_created: number;
    users_updated: number;
    users_deactivated: number;
    errors: string[];
  }>
> {
  try {
    const res = await withRetry(() =>
      adminAPI.post<{
        groups_synced: number;
        users_created: number;
        users_updated: number;
        users_deactivated: number;
        errors: string[];
      }>('/admin/users/sync-cognito', {})
    );
    revalidatePath('/users');
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── getUserAllowedClientsAction ──────────────────────────────────────────────

export async function getUserAllowedClientsAction(
  userId: string,
): Promise<ActionResult<{ clients: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ user_id: string; clients: string[] }>(
        `/admin/users/${userId}/allowed-clients`,
      ),
    );
    return { success: true, data: { clients: res.clients ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setUserAllowedClientsAction ──────────────────────────────────────────────
// empty array = both allowed → DELETE (clears policy). non-empty → PUT allowlist.

export async function setUserAllowedClientsAction(
  userId: string,
  clients: string[],
): Promise<ActionResult<{ clients: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    if (clients.length === 0) {
      await withRetry(() => adminAPI.delete(`/admin/users/${userId}/allowed-clients`));
      return { success: true, data: { clients: [] } };
    }
    const res = await withRetry(() =>
      adminAPI.put<{ user_id: string; clients: string[] }>(
        `/admin/users/${userId}/allowed-clients`,
        { clients },
      ),
    );
    return { success: true, data: { clients: res.clients ?? clients } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── getScopeAllowedClientsAction / setScopeAllowedClientsAction ──────────────
// 팀/조직 단위 앱 접근 정책 (alembic 0038). 우선순위 user > team > org > 제한없음.
// [] = 정책 없음(하위 폴백) — 전면 거부가 아님. 개인 override 와 같은 의미 체계.

type ClientPolicyScope = 'team' | 'organization';

function scopeClientsPath(scope: ClientPolicyScope, scopeId: string): string {
  return scope === 'team'
    ? `/admin/teams/${scopeId}/allowed-clients`
    : `/admin/organizations/${scopeId}/allowed-clients`;
}

export async function getScopeAllowedClientsAction(
  scope: ClientPolicyScope,
  scopeId: string,
): Promise<ActionResult<{ clients: string[] }>> {
  if (!scopeId) return { success: false, error: 'Scope ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ scope_id: string; clients: string[] }>(
        scopeClientsPath(scope, scopeId),
      ),
    );
    return { success: true, data: { clients: res.clients ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

export async function setScopeAllowedClientsAction(
  scope: ClientPolicyScope,
  scopeId: string,
  clients: string[],
): Promise<ActionResult<{ clients: string[] }>> {
  if (!scopeId) return { success: false, error: 'Scope ID is required' };
  try {
    if (clients.length === 0) {
      await withRetry(() => adminAPI.delete(scopeClientsPath(scope, scopeId)));
      return { success: true, data: { clients: [] } };
    }
    const res = await withRetry(() =>
      adminAPI.put<{ scope_id: string; clients: string[] }>(
        scopeClientsPath(scope, scopeId),
        { clients },
      ),
    );
    return { success: true, data: { clients: res.clients ?? clients } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── getUserAllowedModelsAction ───────────────────────────────────────────────
// per-user model whitelist (overrides team). [] = no override → falls back to team.

export async function getUserAllowedModelsAction(
  userId: string,
): Promise<ActionResult<{ modelAliases: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ user_id: string; model_aliases: string[] }>(
        `/admin/users/${userId}/allowed-models`,
      ),
    );
    return { success: true, data: { modelAliases: res.model_aliases ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setUserAllowedModelsAction ───────────────────────────────────────────────
// empty array = clear override → DELETE (falls back to team policy).
// non-empty = PUT whitelist (overrides team). ★ empty ≠ "deny all".

export async function setUserAllowedModelsAction(
  userId: string,
  modelAliases: string[],
): Promise<ActionResult<{ modelAliases: string[] }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    if (modelAliases.length === 0) {
      await withRetry(() => adminAPI.delete(`/admin/users/${userId}/allowed-models`));
      return { success: true, data: { modelAliases: [] } };
    }
    const res = await withRetry(() =>
      adminAPI.put<{ user_id: string; model_aliases: string[] }>(
        `/admin/users/${userId}/allowed-models`,
        { model_aliases: modelAliases },
      ),
    );
    return { success: true, data: { modelAliases: res.model_aliases ?? modelAliases } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── getUserClientBudgetsAction ───────────────────────────────────────────────
// per-app(client) 예산 현재값 조회 — UI prefill 용. Decimal 은 JSON 에서 string.

export async function getUserClientBudgetsAction(
  userId: string,
): Promise<ActionResult<{ apps: Array<{ client: string; max_budget_usd: string; policy: string }> }>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<{ user_id: string; apps: Array<{ client: string; max_budget_usd: string; policy: string }> }>(
        `/admin/budgets/user/${userId}/apps`,
      ),
    );
    return { success: true, data: { apps: res.apps ?? [] } };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── setUserClientBudgetAction ────────────────────────────────────────────────

export async function setUserClientBudgetAction(
  userId: string,
  client: string,
  body: {
    max_budget_usd: string;
    policy?: string;
    alert_thresholds?: number[];
    confirm?: boolean;
  },
): Promise<ActionResult<void>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    await withRetry(() => adminAPI.put(`/admin/budgets/user/${userId}/app/${client}`, body));
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, ...toActionError(err) };
  }
}

// ─── clearUserClientBudgetAction ──────────────────────────────────────────────

export async function clearUserClientBudgetAction(
  userId: string,
  client: string,
): Promise<ActionResult<void>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    // 앱 예산 해제는 한도 완화라 409/confirm 흐름이 없다 — confirm 파라미터를
    // 보내지 않는다(엔드포인트가 받지 않는다).
    await withRetry(() =>
      adminAPI.delete(`/admin/budgets/user/${userId}/app/${client}`),
    );
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, ...toActionError(err) };
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

// ─── searchUsersAction (조직 트리 검색창) ─────────────────────────────────────
//
// 이메일/이름 부분 일치. 2자 미만은 서버를 치지 않고 즉시 빈 결과 — 1자 검색은
// 사실상 전량 매칭이라 비싸고 무의미하다(백엔드도 같은 하한을 갖는다:
// admin-api UserTeamService.SEARCH_MIN_LEN).
//
// ⚠️ 타이핑마다 호출되는 경로이므로 `withRetry` 를 **쓰지 않는다.** 실패한 요청은
//    다음 키 입력이 어차피 대체하므로, 재시도는 결과를 늦게 만들 뿐이다. 다른
//    액션들이 withRetry 를 쓰는 것과 의도적으로 다르다.

const SEARCH_MIN_LEN = 2;

export async function searchUsersAction(
  term: string,
): Promise<ActionResult<{ items: UserSearchItem[]; truncated: boolean }>> {
  const trimmed = term.trim();
  if (trimmed.length < SEARCH_MIN_LEN) {
    return { success: true, data: { items: [], truncated: false } };
  }
  try {
    const res = await adminAPI.get<{ items: UserSearchItem[]; truncated: boolean }>(
      `/admin/users/search?q=${encodeURIComponent(trimmed)}`,
    );
    return {
      success: true,
      data: { items: res.items ?? [], truncated: res.truncated ?? false },
    };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

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
// ─── getEffectivePolicyAction ────────────────────────────────────────────────
// 사용자에게 적용되는 정책의 합성 읽기 전용 뷰 (effective-policy).

import type { EffectivePolicy } from '@/types/entities';

export async function getEffectivePolicyAction(
  userId: string,
): Promise<ActionResult<EffectivePolicy>> {
  if (!userId) return { success: false, error: 'User ID is required' };
  try {
    const res = await withRetry(() =>
      adminAPI.get<EffectivePolicy>(`/admin/users/${userId}/effective-policy`),
    );
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}
