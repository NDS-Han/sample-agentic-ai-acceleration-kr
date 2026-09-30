// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { TeamBudgetAllocation } from '@/types/entities';

// ⚠️ admin-api 는 pydantic Decimal 을 JSON 문자열로 직렬화한다("used_usd":"3").
//    TeamBudgetAllocation 의 필드는 number 로 선언돼 있어, 문자열 그대로
//    렌더하면 `.toFixed()` 크래시가 난다 — 데이터 경계(서버 액션/페이지 fetch)
//    에서 여기로 통과시켜 숫자로 정규화한다.
const num = (v: unknown): number => {
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : 0;
};

const numOrNull = (v: unknown): number | null => {
  if (v == null) return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
};

export function normalizeAllocation(raw: unknown): TeamBudgetAllocation | null {
  if (raw == null || typeof raw !== 'object') return null;
  const a = raw as TeamBudgetAllocation;
  return {
    ...a,
    total_budget_usd: num(a.total_budget_usd),
    default_user_cap_usd: numOrNull(a.default_user_cap_usd),
    sum_allocated_usd: numOrNull(a.sum_allocated_usd),
    overcommit_ratio: numOrNull(a.overcommit_ratio),
    entries: (a.entries ?? []).map((e) => ({
      ...e,
      allocated_usd: num(e.allocated_usd),
      used_usd: num(e.used_usd),
      remaining_usd: num(e.remaining_usd),
      effective_cap_usd: numOrNull(e.effective_cap_usd),
    })),
  };
}
