// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * fmtTokensCompact — 토큰 수의 단일 포맷터.
 *
 * 회귀 배경: 대시보드 KPI 는 로컬 formatTokens(B/M/K, 소수 2자리)를 쓰고
 * ModelsTable/분석 위젯은 fmtTokensCompact(K/M 만)를 써서, 10억 이상 값이
 * '1.05B' 와 '1050M' 으로 갈라졌다. B 분기 추가 후 단일 출처로 통일.
 */

import { describe, it, expect } from 'vitest';
import {
  fmtTokensCompact,
  fmtUsd,
  fmtUsdExact,
  usdTitle,
  fmtUsdRate,
  fmtUsdTick,
  niceTicks,
  fmtLatencyMs,
  roundSharesTo100,
  fmtSharePct,
  fmtDateMinute,
} from '@/lib/utils/format';
import { fmtPricePerM } from '@/lib/utils/pricing';

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

describe('fmtUsd — 단일 비용 포맷', () => {
  it('$0.01 이상은 소수 2자리 + 그룹핑', () => {
    expect(fmtUsd(0)).toBe('$0.00');
    expect(fmtUsd(0.16)).toBe('$0.16');
    expect(fmtUsd(0.01)).toBe('$0.01');
    expect(fmtUsd(1234.5)).toBe('$1,234.50');
  });

  it("0 < n < $0.01 은 '< $0.01' — 소액 자릿수가 화면마다 제각각이던 것 통일", () => {
    expect(fmtUsd(0.0071)).toBe('< $0.01');
    expect(fmtUsd(0.000078)).toBe('< $0.01');
    expect(fmtUsd(0.0000995)).toBe('< $0.01');
    expect(fmtUsd(-0.003)).toBe('> -$0.01');
  });

  it('정확한 값(툴팁)은 유효숫자 2개 — usdTitle 은 줄인 경우에만', () => {
    expect(fmtUsdExact(0.000078)).toBe('$0.000078');
    expect(fmtUsdExact(0.007)).toBe('$0.0070');
    expect(fmtUsdExact(0.16)).toBe('$0.16');
    expect(usdTitle(0.000078)).toBe('$0.000078');
    expect(usdTitle(0.16)).toBeUndefined();
    expect(usdTitle(0)).toBeUndefined();
    expect(usdTitle(null)).toBeUndefined();
  });

  it('반올림이 $0.01 에 닿으면 2자리 규칙으로 간다', () => {
    expect(fmtUsd(0.009996)).toBe('$0.01');
  });

  it('precision 을 명시하면 그 자릿수 고정', () => {
    expect(fmtUsd(0.12345, 4)).toBe('$0.1235');
  });
});

describe('fmtPricePerM — 단가도 같은 규칙', () => {
  it('$2.00/M 과 $0.10/M 이 같은 자릿수 규칙을 따른다', () => {
    expect(fmtPricePerM(0.002)).toBe('$2.00/M');
    expect(fmtPricePerM(0.0001)).toBe('$0.10/M');
    expect(fmtPricePerM(0.015)).toBe('$15.00/M');
  });

  it('의미 있는 소수는 5자리까지 보존한다(US-19)', () => {
    expect(fmtPricePerM(0.0001375)).toBe('$0.1375/M');
    expect(fmtPricePerM(0.000625)).toBe('$0.625/M');
  });

  it('$0.01/M 미만은 유효숫자 2개, 빈 값은 —', () => {
    expect(fmtPricePerM(0.0000071)).toBe('$0.0071/M');
    expect(fmtPricePerM(null)).toBe('—');
    expect(fmtUsdRate(1500)).toBe('$1,500.00');
  });
});

describe('niceTicks / fmtUsdTick', () => {
  it('0.135 → 0, 0.05, 0.10, 0.15', () => {
    const ticks = niceTicks(0.135);
    expect(ticks).toEqual([0, 0.05, 0.1, 0.15]);
    expect(ticks.map((v) => fmtUsdTick(v, 0.05))).toEqual(['$0.00', '$0.05', '$0.10', '$0.15']);
  });

  it('큰 값/2.5 간격/0 입력', () => {
    expect(niceTicks(87)).toEqual([0, 25, 50, 75, 100]);
    expect(niceTicks(0)).toEqual([0, 1]);
    expect(fmtUsdTick(2500, 2500)).toBe('$2.5k');
    expect(fmtUsdTick(0.25, 0.25)).toBe('$0.25');
  });
});

describe('fmtLatencyMs', () => {
  it('1초 이상은 s 소수 1자리, 미만은 ms', () => {
    expect(fmtLatencyMs(55931)).toBe('55.9s');
    expect(fmtLatencyMs(1234567)).toBe('1,234.6s');
    expect(fmtLatencyMs(850)).toBe('850ms');
    expect(fmtLatencyMs(0)).toBe('—');
  });
});

describe('roundSharesTo100 / fmtSharePct', () => {
  it('99.95 + 0.05 를 반올림해도 합이 100.0 이다', () => {
    const r = roundSharesTo100([99.94, 0.06]);
    expect(r).toEqual([99.9, 0.1]);
    expect(r.reduce((a, b) => a + b, 0)).toBeCloseTo(100, 9);
  });

  it('0 으로 내려간 비0 항목은 <0.1%', () => {
    const raw = [99.97, 0.02, 0.01];
    const r = roundSharesTo100(raw);
    expect(r.reduce((a, b) => a + b, 0)).toBeCloseTo(100, 9);
    expect(fmtSharePct(r[2], raw[2])).toBe('<0.1%');
    expect(fmtSharePct(r[0], raw[0])).toBe('100.0%');
  });
});

describe('fmtDateMinute — 연도 포함 분 단위 시점', () => {
  it('주입된 TZ 로 날짜 경계를 넘긴다', () => {
    expect(fmtDateMinute('2026-10-15T15:30:00Z', 'Asia/Seoul')).toBe('2026-10-16 00:30');
    expect(fmtDateMinute('2026-10-15T15:30:00Z', 'Asia/Kolkata')).toBe('2026-10-15 21:00');
    expect(fmtDateMinute('2026-10-15T15:30:00Z', 'UTC')).toBe('2026-10-15 15:30');
  });

  it('파싱 불가 입력은 그대로', () => {
    expect(fmtDateMinute('not-a-date', 'UTC')).toBe('not-a-date');
  });
});
