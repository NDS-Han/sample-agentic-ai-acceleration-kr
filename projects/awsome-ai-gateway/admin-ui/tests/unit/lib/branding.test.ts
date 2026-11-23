// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  brandName,
  DEFAULT_BRAND_NAME,
  MAX_BRAND_NAME_LENGTH,
  resolveBrandName,
} from '@/lib/branding';

describe('resolveBrandName', () => {
  it('미설정·공백이면 기본값', () => {
    expect(resolveBrandName(undefined)).toBe(DEFAULT_BRAND_NAME);
    expect(resolveBrandName(null)).toBe(DEFAULT_BRAND_NAME);
    expect(resolveBrandName('')).toBe(DEFAULT_BRAND_NAME);
    expect(resolveBrandName('   \n\t ')).toBe(DEFAULT_BRAND_NAME);
  });

  it('앞뒤 공백 제거, 내부 연속 공백·개행은 한 칸으로', () => {
    expect(resolveBrandName('  농심   AI\nGateway  ')).toBe('농심 AI Gateway');
  });

  it('상한을 넘으면 코드포인트 단위로 자른다', () => {
    const long = '가'.repeat(MAX_BRAND_NAME_LENGTH + 10);
    expect(Array.from(resolveBrandName(long))).toHaveLength(MAX_BRAND_NAME_LENGTH);
    const emoji = '🚀'.repeat(MAX_BRAND_NAME_LENGTH + 1);
    expect(resolveBrandName(emoji)).toBe('🚀'.repeat(MAX_BRAND_NAME_LENGTH));
  });
});

describe('brandName', () => {
  afterEach(() => vi.unstubAllEnvs());

  it('ADMIN_BRAND_NAME env 를 읽는다', () => {
    vi.stubEnv('ADMIN_BRAND_NAME', 'Nongshim AI Gateway');
    expect(brandName()).toBe('Nongshim AI Gateway');
  });

  it('env 가 없으면 기본값', () => {
    vi.stubEnv('ADMIN_BRAND_NAME', '');
    expect(brandName()).toBe(DEFAULT_BRAND_NAME);
  });
});
