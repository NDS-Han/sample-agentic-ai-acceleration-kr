// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * audit_logs.changes 모양 분류기 — 순수 함수(단위 테스트 대상).
 *
 * 기록 경로마다 changes 형태가 다르다(app/core/audit.py 주석 참조):
 *  - {before:{...}, after:{...}}  필드 diff        → fieldDiff
 *  - {before:[...], after:[...]}  집합 diff        → setDiff
 *  - {before:"..", after:".."}    스칼라           → scalar
 *  - {after:{...}} / {after:[..]} after 만         → afterOnly (이전 값 미기록 — 가짜 diff 금지)
 *  - {_unserializable:true}       직렬화 실패 폴백 → unserializable
 *  - before/after 없는 평평한 dict → kv
 *  - 빈 객체/null                  → empty
 */

export type FieldEntry = { key: string; before: unknown; after: unknown; changed: boolean };

export type ChangesShape =
  | { kind: 'fieldDiff'; fields: FieldEntry[] }
  | { kind: 'setDiff'; added: unknown[]; removed: unknown[]; kept: unknown[] }
  | { kind: 'scalar'; before: unknown; after: unknown }
  | { kind: 'afterOnly'; fields: { key: string; value: unknown }[] }
  | { kind: 'kv'; entries: { key: string; value: unknown }[] }
  | { kind: 'unserializable' }
  | { kind: 'empty' };

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === 'object' && v !== null && !Array.isArray(v);

export function classifyChanges(
  changes: Record<string, unknown> | null | undefined,
): ChangesShape {
  if (!changes || Object.keys(changes).length === 0) return { kind: 'empty' };
  if (changes._unserializable === true) return { kind: 'unserializable' };

  const hasBefore = 'before' in changes;
  const hasAfter = 'after' in changes;

  if (hasBefore || hasAfter) {
    const before = changes.before;
    const after = changes.after;

    if (hasBefore && hasAfter) {
      if (isPlainObject(before) && isPlainObject(after)) {
        const keys = new Set([...Object.keys(before), ...Object.keys(after)]);
        const fields: FieldEntry[] = [...keys].map((key) => ({
          key,
          before: before[key],
          after: after[key],
          changed: JSON.stringify(before[key]) !== JSON.stringify(after[key]),
        }));
        return { kind: 'fieldDiff', fields };
      }
      if (Array.isArray(before) && Array.isArray(after)) {
        const beforeSet = new Set(before.map((v) => JSON.stringify(v)));
        const afterSet = new Set(after.map((v) => JSON.stringify(v)));
        return {
          kind: 'setDiff',
          added: after.filter((v) => !beforeSet.has(JSON.stringify(v))),
          removed: before.filter((v) => !afterSet.has(JSON.stringify(v))),
          kept: after.filter((v) => beforeSet.has(JSON.stringify(v))),
        };
      }
      return { kind: 'scalar', before, after };
    }

    // after 만 — 이전 값이 '없음' 이 아니라 '미기록' 이다(설계 메모 참조).
    if (hasAfter) {
      if (isPlainObject(after)) {
        return {
          kind: 'afterOnly',
          fields: Object.entries(after).map(([key, value]) => ({ key, value })),
        };
      }
      if (Array.isArray(after)) {
        return { kind: 'setDiff', added: after, removed: [], kept: [] };
      }
      return { kind: 'scalar', before: undefined, after };
    }
    // before 만 — CLEAR 가 아니라 드문 형태. 정직하게 제거 diff 로.
    if (Array.isArray(before)) {
      return { kind: 'setDiff', added: [], removed: before, kept: [] };
    }
    if (isPlainObject(before)) {
      return {
        kind: 'kv',
        entries: Object.entries(before).map(([key, value]) => ({ key, value })),
      };
    }
    return { kind: 'scalar', before, after: undefined };
  }

  return {
    kind: 'kv',
    entries: Object.entries(changes).map(([key, value]) => ({ key, value })),
  };
}
