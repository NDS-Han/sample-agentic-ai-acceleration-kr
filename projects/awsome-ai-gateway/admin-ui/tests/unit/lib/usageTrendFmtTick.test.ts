// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * UsageTrendChart.fmtTick — X축/툴팁 라벨의 타임존.
 *
 * 버킷은 epoch 내림 절대 시각 창이라 차트는 브라우저 TZ 를 주입해 그린다.
 * fmtTick 자체는 주입된 TZ 로만 포맷한다(Date.getHours() 등 런타임 getter 금지).
 */

import { describe, it, expect } from 'vitest';
import { fmtTick } from '@/components/rate-limits/UsageTrendChart';

// 2026-10-15T15:30:00Z — Asia/Seoul 로는 10/16 00:30 (자정 경계 넘음).
const T = Date.UTC(2026, 9, 15, 15, 30, 0) / 1000;
// 시간 버킷 경계(UTC 정시) — 2026-10-15T15:00:00Z.
const H = Date.UTC(2026, 9, 15, 15, 0, 0) / 1000;

describe('fmtTick injected timezone', () => {
  it('labels sub-hour buckets in the reporting timezone, not browser TZ', () => {
    expect(fmtTick(T, 60, 'Asia/Seoul')).toBe('00:30');
    expect(fmtTick(T, 60, 'UTC')).toBe('15:30');
  });

  it('crosses the date boundary correctly for hourly buckets', () => {
    expect(fmtTick(H, 3600, 'Asia/Seoul')).toBe('10/16 00시');
    expect(fmtTick(H, 3600, 'UTC')).toBe('10/15 15시');
  });

  it('keeps minutes for half-hour offset zones instead of rounding to the hour', () => {
    // UTC 정시 버킷이 IST 에서는 :30 에 떨어진다 — '21시' 로 그리면 30분 틀린다.
    expect(fmtTick(H, 3600, 'Asia/Kolkata', 'ko')).toBe('10/15 20:30');
    expect(fmtTick(H, 3600, 'Asia/Kolkata', 'en')).toBe('10/15 20:30');
    expect(fmtTick(H, 3600, 'Asia/Seoul', 'en')).toBe('10/16 00:00');
  });

  it('falls back to runtime timezone when none is given', () => {
    // timeZone 미지정 — 브라우저/런타임 TZ. 형식만 검증한다.
    expect(fmtTick(T, 60)).toMatch(/^\d{2}:\d{2}$/);
    expect(fmtTick(H, 3600)).toMatch(/^\d{1,2}\/\d{1,2} \d{2}(시|:\d{2})$/);
  });
});
