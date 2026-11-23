// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { describe, expect, it } from 'vitest';
import { DEFAULT_LOCALE, localeFromAcceptLanguage, resolveLocale } from '@/i18n/locale';

describe('localeFromAcceptLanguage', () => {
  it('지역 태그는 기본 언어로 — en-US → en, ko-KR → ko', () => {
    expect(localeFromAcceptLanguage('en-US,en;q=0.9')).toBe('en');
    expect(localeFromAcceptLanguage('ko-KR,ko;q=0.9,en-US;q=0.8')).toBe('ko');
  });

  it('q 값 높은 순으로, 지원하지 않는 언어는 건너뛴다', () => {
    expect(localeFromAcceptLanguage('fr-FR,fr;q=0.9,en;q=0.8,ko;q=0.7')).toBe('en');
    expect(localeFromAcceptLanguage('en;q=0.5, ko;q=0.8')).toBe('ko');
  });

  it('q=0 은 제외, 지원 언어가 없거나 헤더가 없으면 null', () => {
    expect(localeFromAcceptLanguage('ko;q=0, en;q=0.1')).toBe('en');
    expect(localeFromAcceptLanguage('fr,de')).toBeNull();
    expect(localeFromAcceptLanguage('')).toBeNull();
    expect(localeFromAcceptLanguage(undefined)).toBeNull();
  });
});

describe('resolveLocale', () => {
  it('KO/EN 으로 고른 쿠키가 브라우저 언어보다 우선', () => {
    expect(resolveLocale('ko', 'en-US,en;q=0.9')).toBe('ko');
    expect(resolveLocale('en', 'ko-KR')).toBe('en');
  });

  it('쿠키가 없거나 지원 밖이면 브라우저 언어, 그것도 없으면 기본값', () => {
    expect(resolveLocale(undefined, 'en-US,en;q=0.9')).toBe('en');
    expect(resolveLocale('fr', 'en-US')).toBe('en');
    expect(resolveLocale(undefined, 'fr')).toBe(DEFAULT_LOCALE);
    expect(resolveLocale(undefined)).toBe(DEFAULT_LOCALE);
  });
});
