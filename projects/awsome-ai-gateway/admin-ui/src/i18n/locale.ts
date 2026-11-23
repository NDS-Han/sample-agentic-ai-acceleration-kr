// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * locale 결정의 단일 규칙 — i18n/request.ts 와 app/layout.tsx 가 함께 쓴다.
 * 이 규칙이 두 곳에 따로 있으면 한쪽만 고쳐져 `lang="fr"` + ko 메시지 같은
 * 조합이 생긴다(실제로 layout.tsx 가 원시 쿠키값을 쓰던 사례).
 */
export const SUPPORTED_LOCALES = ['ko', 'en'] as const;
export type Locale = (typeof SUPPORTED_LOCALES)[number];
export const DEFAULT_LOCALE: Locale = 'ko';

function isSupported(v: string): v is Locale {
  return (SUPPORTED_LOCALES as readonly string[]).includes(v);
}

/**
 * Accept-Language 헤더에서 지원 locale 을 고른다 — q 값 높은 순, 지역 태그는
 * 기본 언어로(en-US → en). 지원 언어가 없으면 null.
 */
export function localeFromAcceptLanguage(header: string | undefined | null): Locale | null {
  if (!header) return null;
  const ranked = header
    .split(',')
    .map((part, i) => {
      const [tag, ...params] = part.trim().split(';');
      const q = params
        .map((p) => p.trim())
        .find((p) => p.startsWith('q='));
      const qv = q ? Number(q.slice(2)) : 1;
      return { lang: tag.trim().toLowerCase().split('-')[0], q: Number.isFinite(qv) ? qv : 0, i };
    })
    .filter((x) => x.lang && x.q > 0)
    .sort((a, b) => b.q - a.q || a.i - b.i);
  return ranked.find((x) => isSupported(x.lang))?.lang as Locale | undefined ?? null;
}

/**
 * locale 결정 — ① 사용자가 KO/EN 으로 고른 locale 쿠키 ② 브라우저 언어
 * (Accept-Language) ③ DEFAULT_LOCALE. 쿠키 값이 목록에 없으면 없는 것으로 본다.
 */
export function resolveLocale(
  raw: string | undefined | null,
  acceptLanguage?: string | null,
): Locale {
  if (raw && isSupported(raw)) return raw;
  return localeFromAcceptLanguage(acceptLanguage) ?? DEFAULT_LOCALE;
}
