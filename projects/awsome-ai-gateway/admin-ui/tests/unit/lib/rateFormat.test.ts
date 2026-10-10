/**
 * 단가 표시 — 소수 8자리까지 보이고 끝의 0 은 줄인다(최소 4자리). US-19, 2026-10-10.
 * Haiku 5.5 의 단가(0.00011, 0.0001375, 0.000011)가 toFixed(4/5/6) 로 잘려 보이던 것을 고친다.
 */
import { describe, it, expect } from 'vitest';
import { formatRate } from '@/lib/utils/rateFormat';

describe('formatRate', () => {
  it.each([
    [0.003, '0.0030'],
    [0.015, '0.0150'],
    [0.00011, '0.00011'],
    [0.0001375, '0.0001375'],
    [0.000011, '0.000011'],
    [0.00000001, '0.00000001'],
    [1.5, '1.5000'],
  ])('%s -> %s', (n, s) => {
    expect(formatRate(n)).toBe(s);
  });
});
