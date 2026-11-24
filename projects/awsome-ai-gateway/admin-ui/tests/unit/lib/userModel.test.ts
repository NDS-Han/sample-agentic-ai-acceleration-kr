// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** 사용자 × 모델 표의 순수 헬퍼 — 히트 스케일·셀 값·정렬 파라미터·쿼리. */

import { describe, expect, it } from 'vitest';
import {
  cellTooltip,
  cellValue,
  filterParams,
  heatBackground,
  heatIntensity,
  isUuid,
  nextSort,
  toSortParam,
  tokenMix,
  userSubline,
  type UserModelCell,
} from '@/components/analytics/userModel';

const CELL: UserModelCell = {
  input_tokens: 100,
  output_tokens: 50,
  cache_read_tokens: 300,
  cache_write_tokens: 50,
  total_tokens: 500,
  cost_usd: 0.004,
  requests: 3,
};

describe('heatIntensity — sqrt 스케일, 0..1', () => {
  it('sqrt(value / max)', () => {
    expect(heatIntensity(25, 100)).toBeCloseTo(0.5);
    expect(heatIntensity(100, 100)).toBe(1);
    expect(heatIntensity(1, 100)).toBeCloseTo(0.1);
  });
  it('max 초과는 1 로, 0·음수·NaN·max 0 은 0', () => {
    expect(heatIntensity(400, 100)).toBe(1);
    expect(heatIntensity(0, 100)).toBe(0);
    expect(heatIntensity(-5, 100)).toBe(0);
    expect(heatIntensity(Number.NaN, 100)).toBe(0);
    expect(heatIntensity(10, 0)).toBe(0);
  });
  it('농도 0 은 배경 없음, 그 외는 primary 틴트(알파 상한 0.46)', () => {
    expect(heatBackground(0)).toBeUndefined();
    expect(heatBackground(1)).toBe('hsl(var(--primary) / 0.460)');
    expect(heatBackground(0.5)).toBe('hsl(var(--primary) / 0.260)');
  });
});

describe('cellValue', () => {
  it('모드별 값 — cache = read + write', () => {
    expect(cellValue(CELL, 'total')).toBe(500);
    expect(cellValue(CELL, 'input')).toBe(100);
    expect(cellValue(CELL, 'output')).toBe(50);
    expect(cellValue(CELL, 'cache')).toBe(350);
  });
  it('미사용(null/undefined) 은 0', () => {
    expect(cellValue(null, 'total')).toBe(0);
    expect(cellValue(undefined, 'cache')).toBe(0);
  });
});

describe('정렬 파라미터', () => {
  it('Total 열은 활성 토큰 모드를 따른다', () => {
    expect(toSortParam('total', 'total')).toBe('total');
    expect(toSortParam('total', 'input')).toBe('input');
    expect(toSortParam('total', 'cache')).toBe('cache');
  });
  it('그 외 열은 그대로 — 모델 열은 model:<alias>', () => {
    expect(toSortParam('model:claude-sonnet-4-6', 'output')).toBe('model:claude-sonnet-4-6');
    expect(toSortParam('name', 'input')).toBe('name');
    expect(toSortParam('other', 'input')).toBe('other');
    expect(toSortParam('cost', 'cache')).toBe('cost');
  });
  it('같은 열 클릭은 방향 반전, 새 열은 이름=asc·숫자=desc', () => {
    expect(nextSort({ col: 'total', dir: 'desc' }, 'total')).toEqual({ col: 'total', dir: 'asc' });
    expect(nextSort({ col: 'total', dir: 'asc' }, 'total')).toEqual({ col: 'total', dir: 'desc' });
    expect(nextSort({ col: 'total', dir: 'desc' }, 'name')).toEqual({ col: 'name', dir: 'asc' });
    expect(nextSort({ col: 'name', dir: 'asc' }, 'model:x')).toEqual({ col: 'model:x', dir: 'desc' });
  });
});

describe('filterParams', () => {
  it('월 + scope 기본 all, client all 은 생략', () => {
    expect(filterParams({ period: '2026-11', scope: '', client: 'all' }).toString()).toBe(
      'period=2026-11&scope=all',
    );
  });
  it('custom 구간은 두 날짜가 모두 있을 때만', () => {
    expect(
      filterParams({ period: '2026-11', start_date: '2026-11-01', end_date: '2026-11-10', scope: 'team:abc', client: 'codex' }).toString(),
    ).toBe('period=2026-11&scope=team%3Aabc&start_date=2026-11-01&end_date=2026-11-10&client=codex');
    expect(filterParams({ period: '2026-11', start_date: '2026-11-01', scope: 'all' }).has('start_date')).toBe(false);
  });
});

describe('툴팁·구성·UUID', () => {
  it('셀 툴팁은 입력/출력/캐시 읽기/쓰기/합계/비용/요청을 담는다', () => {
    const tip = cellTooltip('Sonnet · Kim', CELL, {
      input: 'Input',
      output: 'Output',
      cacheRead: 'Cache read',
      cacheWrite: 'Cache write',
      total: 'Total',
      requests: (n) => `${n} ${n === 1 ? 'request' : 'requests'}`,
    });
    expect(tip.split('\n')).toEqual([
      'Sonnet · Kim',
      'Input 100 · Output 50',
      'Cache read 300 · Cache write 50',
      'Total 500',
      '$0.0040 · 3 requests',
    ]);
    const big = cellTooltip('t', { ...CELL, total_tokens: 143_251, cost_usd: 0.13, requests: 4 }, {
      input: 'Input', output: 'Output', cacheRead: 'Cache read', cacheWrite: 'Cache write', total: 'Total',
      requests: (n) => `${n} requests`,
    });
    expect(big.split('\n').slice(-2)).toEqual(['Total 143K', '$0.13 · 4 requests']);
  });
  it('userSubline — "email · team", 이름이 없으면 이메일은 이름 자리라 팀만', () => {
    expect(userSubline({ name: 'Load Test', email: 'lt1@x.io', team_name: 'QA' })).toBe('lt1@x.io · QA');
    expect(userSubline({ name: 'Load Test', email: 'lt2@x.io', team_name: null })).toBe('lt2@x.io');
    expect(userSubline({ name: ' ', email: 'a@x.io', team_name: 'QA' })).toBe('QA');
    expect(userSubline({ name: null, email: 'a@x.io' })).toBe('');
  });
  it('tokenMix 는 버킷 비율(%)', () => {
    expect(tokenMix(CELL)).toEqual([20, 10, 60, 10]);
    expect(tokenMix({ ...CELL, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0 })).toEqual([0, 0, 0, 0]);
  });
  it('isUuid', () => {
    expect(isUuid('6f1c0b4e-8a1d-4c2e-9f3a-0b1c2d3e4f50')).toBe(true);
    expect(isUuid('../admin')).toBe(false);
    expect(isUuid('')).toBe(false);
  });
});
