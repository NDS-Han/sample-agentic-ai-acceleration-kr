// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * fmtTokensCompact — 토큰 수의 단일 포맷터.
 *
 * 회귀 배경: 대시보드 KPI 는 로컬 formatTokens(B/M/K, 소수 2자리)를 쓰고
 * ModelsTable/분석 위젯은 fmtTokensCompact(K/M 만)를 써서, 10억 이상 값이
 * '1.05B' 와 '1050M' 으로 갈라졌다. B 분기 추가 후 단일 출처로 통일.
 */

import { describe, it, expect } from 'vitest';
import { fmtTokensCompact } from '@/lib/utils/format';

describe('fmtTokensCompact', () => {
  it('renders billions with B suffix', () => {
    expect(fmtTokensCompact(1_050_000_000)).toBe('1.1B');
    expect(fmtTokensCompact(2_000_000_000)).toBe('2B');
    expect(fmtTokensCompact(12_300_000_000)).toBe('12B');
  });

  it('renders millions with M suffix', () => {
    expect(fmtTokensCompact(1_500_000)).toBe('1.5M');
    expect(fmtTokensCompact(15_000_000)).toBe('15M');
    // 경계 — 999,999,999 는 M 이 아니라 B 로 넘어가지 않는다.
    expect(fmtTokensCompact(999_999_999)).toBe('1000M');
  });

  it('renders thousands with K suffix and small values verbatim', () => {
    expect(fmtTokensCompact(1_500)).toBe('2K');
    expect(fmtTokensCompact(999)).toBe('999');
    expect(fmtTokensCompact(1)).toBe('1');
  });

  it('returns em-dash for nullish, non-finite, and non-positive', () => {
    expect(fmtTokensCompact(null)).toBe('—');
    expect(fmtTokensCompact(undefined)).toBe('—');
    expect(fmtTokensCompact(0)).toBe('—');
    expect(fmtTokensCompact(-5)).toBe('—');
    expect(fmtTokensCompact(NaN)).toBe('—');
    expect(fmtTokensCompact(Infinity)).toBe('—');
  });
});
