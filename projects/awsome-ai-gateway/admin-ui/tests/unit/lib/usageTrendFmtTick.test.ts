// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * UsageTrendChart.fmtTick — X축/툴팁 라벨의 타임존.
 *
 * 회귀 배경: Date.getHours() 등 브라우저 로컬 getter 를 써서, 서버가 리포팅 TZ
 * (Asia/Seoul)로 자른 버킷이 다른 TZ 의 브라우저에서는 다른 시각으로 라벨링됐다.
 * 이제 Intl.DateTimeFormat + 주입된 리포팅 TZ 로 포맷한다.
 */

import { describe, it, expect } from 'vitest';
import { fmtTick } from '@/components/rate-limits/UsageTrendChart';

// 2026-10-15T15:30:00Z — Asia/Seoul 로는 10/16 00:30 (자정 경계 넘음).
const T = Date.UTC(2026, 9, 15, 15, 30, 0) / 1000;

describe('fmtTick reporting-timezone', () => {
  it('labels sub-hour buckets in the reporting timezone, not browser TZ', () => {
    expect(fmtTick(T, 60, 'Asia/Seoul')).toBe('00:30');
    expect(fmtTick(T, 60, 'UTC')).toBe('15:30');
  });

  it('crosses the date boundary correctly for hourly buckets', () => {
    expect(fmtTick(T, 3600, 'Asia/Seoul')).toBe('10/16 00시');
    expect(fmtTick(T, 3600, 'UTC')).toBe('10/15 15시');
  });

  it('falls back to runtime timezone when none is given', () => {
    // timeZone 미지정 — 브라우저/런타임 TZ. 형식만 검증한다.
    expect(fmtTick(T, 60)).toMatch(/^\d{2}:\d{2}$/);
    expect(fmtTick(T, 3600)).toMatch(/^\d{1,2}\/\d{1,2} \d{2}시$/);
  });
});
