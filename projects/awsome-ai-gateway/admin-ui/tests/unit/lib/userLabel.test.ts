// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { describe, expect, it } from 'vitest';
import { isSyntheticEmail, userLabel } from '@/lib/userLabel';

const SYN = '61739d0a-1111-2222-3333-444455556666@unknown';

describe('isSyntheticEmail', () => {
  it('<sub>@unknown 은 합성 이메일이다', () => {
    expect(isSyntheticEmail(SYN)).toBe(true);
    expect(isSyntheticEmail('ABC@UNKNOWN')).toBe(true);
  });

  it('실제 이메일·빈 값은 합성이 아니다', () => {
    expect(isSyntheticEmail('heomk@example.com')).toBe(false);
    expect(isSyntheticEmail('a@unknown.example.com')).toBe(false);
    expect(isSyntheticEmail(null)).toBe(false);
    expect(isSyntheticEmail(undefined)).toBe(false);
    expect(isSyntheticEmail('')).toBe(false);
  });
});

describe('userLabel', () => {
  it('실제 이메일이면 이메일', () => {
    expect(userLabel({ email: 'heomk@example.com', display_name: '허민규' })).toBe('heomk@example.com');
  });

  it('합성 이메일이면 display_name 우선', () => {
    expect(userLabel({ email: SYN, display_name: '허민규' })).toBe('허민규');
  });

  it('합성 이메일 + 이름 없음이면 식별자 앞 8자리', () => {
    expect(userLabel({ email: SYN, display_name: '' })).toBe('61739d0a…');
    expect(userLabel({ email: SYN, display_name: SYN })).toBe('61739d0a…');
  });

  it('이메일이 없으면 display_name, 둘 다 없으면 빈 문자열', () => {
    expect(userLabel({ email: null, display_name: 'Kim' })).toBe('Kim');
    expect(userLabel({ email: null, display_name: null })).toBe('');
  });
});
