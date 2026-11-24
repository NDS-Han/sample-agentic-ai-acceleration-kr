// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** 허용 모델 대화상자 — 계열별 묶음, 이름 중복 표시, 검색·필터. 순수 함수. */

import type { ModelListItem } from '@/types/entities';

export const FAMILY_ORDER = ['haiku', 'sonnet', 'opus', 'gpt', 'open'] as const;
export type ModelFamily = (typeof FAMILY_ORDER)[number];

/** 별칭·표시명·upstream id 로 계열을 정한다. 어느 것도 아니면 'open'(오픈 모델 등). */
export function familyOf(m: Pick<ModelListItem, 'alias' | 'model_id'> & { display_name?: string | null }): ModelFamily {
  const s = `${m.alias} ${m.display_name ?? ''} ${m.model_id ?? ''}`.toLowerCase();
  if (s.includes('haiku')) return 'haiku';
  if (s.includes('sonnet')) return 'sonnet';
  if (s.includes('opus')) return 'opus';
  if (/\bgpt|codex|\bo[134]\b|openai/.test(s)) return 'gpt';
  return 'open';
}

export function displayLabel(m: { alias: string; display_name?: string | null }): string {
  return m.display_name?.trim() || m.alias;
}

/** 같은 표시명을 가진 별칭이 둘 이상인 표시명 집합 — "이름 중복" 배지용. */
export function duplicateNames(models: { alias: string; display_name?: string | null }[]): Set<string> {
  const count = new Map<string, number>();
  for (const m of models) {
    const k = displayLabel(m);
    count.set(k, (count.get(k) ?? 0) + 1);
  }
  return new Set([...count.entries()].filter(([, n]) => n > 1).map(([k]) => k));
}

export type ModelFilter = 'all' | 'allowed' | 'blocked';

export interface ModelGroup {
  family: ModelFamily;
  /** 그룹 전체(필터와 무관) — 그룹 전체 선택·카운트용. */
  all: ModelListItem[];
  /** 검색·필터를 통과한 항목. */
  items: ModelListItem[];
}

export function groupModels(
  models: ModelListItem[],
  checked: ReadonlySet<string>,
  query: string,
  filter: ModelFilter,
): ModelGroup[] {
  const q = query.trim().toLowerCase();
  const match = (m: ModelListItem) =>
    !q || m.alias.toLowerCase().includes(q) || displayLabel(m).toLowerCase().includes(q);
  const pass = (m: ModelListItem) =>
    filter === 'all' || (filter === 'allowed' ? checked.has(m.alias) : !checked.has(m.alias));
  const groups = new Map<ModelFamily, ModelListItem[]>();
  for (const m of models) {
    const f = familyOf(m);
    groups.set(f, [...(groups.get(f) ?? []), m]);
  }
  return FAMILY_ORDER.filter((f) => groups.has(f))
    .map((f) => {
      const all = [...(groups.get(f) ?? [])].sort(
        (a, b) => displayLabel(a).localeCompare(displayLabel(b)) || a.alias.localeCompare(b.alias),
      );
      return { family: f, all, items: all.filter((m) => match(m) && pass(m)) };
    })
    .filter((g) => g.items.length > 0);
}

/** 두 집합의 차이 개수(추가 + 제거). */
export function changedCount(a: ReadonlySet<string>, b: ReadonlySet<string>): number {
  let n = 0;
  for (const x of a) if (!b.has(x)) n++;
  for (const x of b) if (!a.has(x)) n++;
  return n;
}
