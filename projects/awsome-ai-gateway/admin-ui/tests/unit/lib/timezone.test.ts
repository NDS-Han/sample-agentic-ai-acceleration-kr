// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 시간대 라벨 — 브라우저 TZ 시점 시각과 리포팅 TZ 집계 경계를 헷갈리지 않게
 * 짧은 이름을 붙인다. 이름은 IANA TZ 에서 Intl 로 유도한다(하드코딩 없음).
 */

import { describe, expect, it } from 'vitest';

import { tzAbbr, tzZoneText, withTzAbbr } from '@/lib/utils/timezone';

const NOV = new Date('2026-11-06T03:43:00Z');
const JUL = new Date('2026-07-06T03:43:00Z');

describe('tzAbbr', () => {
  it('약어가 있는 존은 약어 — 리포팅 TZ Asia/Kolkata 는 IST', () => {
    expect(tzAbbr('Asia/Kolkata', NOV)).toBe('IST');
    expect(tzAbbr('UTC', NOV)).toBe('UTC');
  });

  it('약어가 없으면 오프셋 이름(GMT+9) — 허용된 형태', () => {
    expect(tzAbbr('Asia/Seoul', NOV)).toMatch(/^(KST|GMT\+9)$/);
  });

  it('서머타임은 그 시점 기준 — 같은 존이라도 날짜에 따라 이름이 다르다', () => {
    expect(tzAbbr('America/Los_Angeles', NOV)).toBe('PST');
    expect(tzAbbr('America/Los_Angeles', JUL)).toBe('PDT');
  });

  it('잘못된 TZ·날짜에도 던지지 않는다', () => {
    expect(tzAbbr('Not/AZone', NOV)).toBe('Not/AZone');
    expect(tzAbbr('UTC', new Date('garbage'))).toBe('UTC');
  });
});

describe('tzZoneText — 캡션용', () => {
  it("'IST (Asia/Kolkata)' 처럼 약어 + IANA 이름", () => {
    expect(tzZoneText('Asia/Kolkata', NOV)).toBe('IST (Asia/Kolkata)');
  });

  it('약어가 IANA 이름과 같으면 한 번만', () => {
    expect(tzZoneText('UTC', NOV)).toBe('UTC');
  });
});

describe('withTzAbbr — 시각 뒤에 그 시점의 TZ 이름', () => {
  it('ISO 시점으로 서머타임까지 반영한다', () => {
    expect(withTzAbbr('07/06 20:43', 'America/Los_Angeles', JUL.toISOString())).toBe(
      '07/06 20:43 PDT',
    );
  });

  it('빈 문자열은 그대로(마운트 전 자리)', () => {
    expect(withTzAbbr('', 'Asia/Kolkata')).toBe('');
  });
});
