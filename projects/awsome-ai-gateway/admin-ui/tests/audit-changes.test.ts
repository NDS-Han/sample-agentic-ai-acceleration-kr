// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { describe, expect, it } from 'vitest';
import { classifyChanges } from '@/lib/audit/changes';

describe('classifyChanges', () => {
  it('empty/null → empty', () => {
    expect(classifyChanges(null)).toEqual({ kind: 'empty' });
    expect(classifyChanges({})).toEqual({ kind: 'empty' });
  });

  it('_unserializable 폴백', () => {
    expect(classifyChanges({ _unserializable: true })).toEqual({
      kind: 'unserializable',
    });
  });

  it('before·after 객체 → fieldDiff (변경 행만 표시)', () => {
    const s = classifyChanges({
      before: { alias: 'a', rpm: 10 },
      after: { alias: 'a', rpm: 20 },
    });
    expect(s.kind).toBe('fieldDiff');
    if (s.kind === 'fieldDiff') {
      expect(s.fields.find((f) => f.key === 'rpm')?.changed).toBe(true);
      expect(s.fields.find((f) => f.key === 'alias')?.changed).toBe(false);
    }
  });

  it('before·after 배열 → setDiff 추가/제거/유지', () => {
    const s = classifyChanges({
      before: ['a', 'b'],
      after: ['b', 'c'],
    });
    expect(s).toEqual({
      kind: 'setDiff',
      added: ['c'],
      removed: ['a'],
      kept: ['b'],
      extra: [],
    });
  });

  it('CLEAR 형태 (before 배열 + after 빈 배열)', () => {
    const s = classifyChanges({ before: ['a'], after: [] });
    expect(s.kind).toBe('setDiff');
    if (s.kind === 'setDiff') expect(s.removed).toEqual(['a']);
  });

  it('스칼라 before/after → scalar', () => {
    const s = classifyChanges({ before: '12.3', after: '15.0' });
    expect(s).toEqual({ kind: 'scalar', before: '12.3', after: '15.0', extra: [] });
  });

  it('after 객체만 → afterOnly (이전 값 미기록)', () => {
    const s = classifyChanges({ after: { alias: 'x', rpm: 10 } });
    expect(s.kind).toBe('afterOnly');
    if (s.kind === 'afterOnly') expect(s.fields).toHaveLength(2);
  });

  it('after 배열만 → 전부 추가 setDiff', () => {
    const s = classifyChanges({ after: ['a', 'b'] });
    expect(s).toEqual({ kind: 'setDiff', added: ['a', 'b'], removed: [], kept: [], extra: [] });
  });

  it('before 배열만 → 전부 제거 setDiff', () => {
    const s = classifyChanges({ before: ['a'] });
    expect(s).toEqual({ kind: 'setDiff', added: [], removed: ['a'], kept: [], extra: [] });
  });

  it('before/after 없는 평평한 dict → kv', () => {
    const s = classifyChanges({ log_group: 'g', found: true });
    expect(s.kind).toBe('kv');
    if (s.kind === 'kv') expect(s.entries).toHaveLength(2);
  });
});


describe('classifyChanges — extra(형제 키) 보존', () => {
  it('DELETE_MODEL 형태: before + deleted_children → kv 에 전부 보존', () => {
    const s = classifyChanges({
      before: { alias: 'x' },
      deleted_children: { pricings: 3, team_allowed: 2 },
    });
    // before 만 있는 dict 는 kv 로 풀리되 형제 키도 함께 보존된다.
    expect(s.kind).toBe('kv');
    if (s.kind === 'kv') {
      expect(s.entries).toContainEqual({
        key: 'deleted_children',
        value: { pricings: 3, team_allowed: 2 },
      });
      expect(s.entries).toContainEqual({ key: 'alias', value: 'x' });
    }
  });

  it('스칼라 + 부가 키 → scalar + extra', () => {
    const s = classifyChanges({ before: 'a', after: 'b', note: 'n' });
    expect(s).toEqual({
      kind: 'scalar',
      before: 'a',
      after: 'b',
      extra: [{ key: 'note', value: 'n' }],
    });
  });
});
