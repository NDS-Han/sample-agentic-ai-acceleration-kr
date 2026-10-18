'use server';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { revalidatePath } from 'next/cache';
import { z } from 'zod';
import { adminAPI } from '@/lib/api-client';
import { RateLimitSetSchema } from '@/types/api';
import { APIError } from '@/lib/utils/retry';
import type { ActionResult } from './types';

// ─── setRateLimitAction ───────────────────────────────────────────────────────

export async function setRateLimitAction(formData: unknown): Promise<ActionResult<void>> {
  const parsed = RateLimitSetSchema.safeParse(formData);

  if (!parsed.success) {
    const fieldErrors: Record<string, string> = {};
    for (const issue of parsed.error.issues) {
      const key = issue.path.join('.');
      fieldErrors[key] = issue.message;
    }
    return { success: false, error: 'Validation failed', fieldErrors };
  }

  const { target_id, scope, ...limits } = parsed.data;

  try {
    await adminAPI.put(`/admin/rate-limits/${encodeURIComponent(String(scope.toLowerCase()))}/${encodeURIComponent(String(target_id))}`, limits);
    // 편집 지점은 /users 패널 — 옛 /rate-limits 페이지는 리다이렉트다.
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── 단건 조회 / 개별설정 해제 — /users 패널용 ──────────────────────────────────

export interface RateLimitScopeStatusData {
  /** 이 스코프에 직접 설정된 활성 설정 — 없으면 상속/무제한. */
  own: {
    rpm: number | null;
    tpm: number | null;
    cpm: number | null;
    cph: number | null;
  } | null;
  /** USER 전용 — own 이 없을 때 적용되는 팀 설정. */
  inherited: {
    rpm: number | null;
    tpm: number | null;
    cpm: number | null;
    cph: number | null;
  } | null;
  inherited_scope: 'TEAM' | null;
}

export async function getRateLimitStatusAction(
  scope: 'user' | 'team',
  scopeId: string
): Promise<ActionResult<RateLimitScopeStatusData>> {
  try {
    const data = await adminAPI.get<RateLimitScopeStatusData>(
      `/admin/rate-limits/${encodeURIComponent(String(scope))}/${encodeURIComponent(String(scopeId))}`
    );
    // 백엔드가 Decimal 을 JSON 문자열("5.00")로 직렬화한다 — 타입은 number 를
    // 주장하지만 wire 는 string 이라 차트에서 v.toFixed() 가 TypeError 로
    // 터진다(R25-S8). 경계에서 한 번 숫자로 정규화한다.
    if (data.own) {
      data.own = {
        rpm: numOrNull(data.own.rpm),
        tpm: numOrNull(data.own.tpm),
        cpm: numOrNull(data.own.cpm),
        cph: numOrNull(data.own.cph),
      };
    }
    if (data.inherited) {
      data.inherited = {
        rpm: numOrNull(data.inherited.rpm),
        tpm: numOrNull(data.inherited.tpm),
        cpm: numOrNull(data.inherited.cpm),
        cph: numOrNull(data.inherited.cph),
      };
    }
    return { success: true, data };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

/** own 설정 해제 → USER 는 팀 상속, TEAM 은 제한 없음으로 복귀. 멱등. */
export async function deleteRateLimitAction(
  scope: 'user' | 'team',
  scopeId: string
): Promise<ActionResult<void>> {
  try {
    await adminAPI.delete(`/admin/rate-limits/${encodeURIComponent(String(scope))}/${encodeURIComponent(String(scopeId))}`);
    revalidatePath('/users');
    return { success: true, data: undefined };
  } catch (err) {
    return { success: false, error: toErrorMessage(err) };
  }
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

/** Decimal→JSON 문자열 등을 number|null 로. 파싱 불가는 null (0 으로 접지 않는다). */
function numOrNull(v: unknown): number | null {
  if (v == null) return null;
  // Number("") === 0 — 빈 문자열을 0 으로 오인하면 "한도 미설정"이 "$0"으로 보인다.
  if (typeof v === 'string' && v.trim() === '') return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
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