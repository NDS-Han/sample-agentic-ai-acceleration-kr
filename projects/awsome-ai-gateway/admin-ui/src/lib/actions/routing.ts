// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
'use server';

import { adminAPI } from '@/lib/api-client';
import type { ActionResult } from './types';

function toErrorMessage(err: unknown): string {
  if (err && typeof err === 'object' && 'message' in err) {
    return String((err as { message: unknown }).message);
  }
  return typeof err === 'string' ? err : 'Unknown error';
}

export interface RoutingProfileItem {
  client: string; // 'claude-code' | 'cowork' | 'codex'
  web_search_enabled: boolean;
  backend: string;
  enabled: boolean;
  account_role_arn: string | null;
  external_id: string | null;
  region: string;
}

// ─── per-client cross-account routing ────────────────────────────────────────
export interface ClientRoutingInput {
  account_role_arn: string;  // 비우면 in-account 복귀
  external_id: string;       // 선택 — 롤 trust 의 sts:ExternalId
  region: string;            // 필수 — 프로필 기본 리전 컬럼이 NOT NULL
}

export async function setClientRoutingAction(
  client: string,
  input: ClientRoutingInput,
): Promise<ActionResult<ClientRoutingInput & { client: string }>> {
  if (!client) return { success: false, error: 'client is required' };
  try {
    const res = await adminAPI.put<ClientRoutingInput & { client: string }>(
      `/admin/routing-profiles/${encodeURIComponent(String(client))}/routing`,
      {
        account_role_arn: input.account_role_arn || null,
        external_id: input.external_id || null,
        region: input.region,
      },
    );
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── toggle per-client web search ────────────────────────────────────────────
export async function setClientWebSearchAction(
  client: string,
  enabled: boolean,
): Promise<ActionResult<{ client: string; web_search_enabled: boolean }>> {
  if (!client) return { success: false, error: 'client is required' };
  try {
    // 쓰기는 재시도하지 않는다 — 응답 유실 후 재시도 시 감사 로그가 중복된다(R25-N-UI5).
    const res = await adminAPI.put<{ client: string; web_search_enabled: boolean }>(
      `/admin/routing-profiles/${encodeURIComponent(String(client))}/web-search`,
      { enabled },
    );
    // revalidatePath('/apps') 금지 — /apps 는 서버 fetch 데이터가 없어 무효화할 것이 없고,
    // 현재 라우트 리페치가 패널 리마운트(선택 앱 유실)를 일으켰다.
    return { success: true, data: res };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}
