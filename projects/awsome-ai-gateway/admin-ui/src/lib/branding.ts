// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 관리 화면 브랜드명 — 사이드바 로고 옆 이름, 사이드바 하단, 브라우저 탭 제목.
 *
 * 값은 런타임 env ADMIN_BRAND_NAME (NDS gateway.yaml 의 branding.name 을
 * compose/ecs/eks 렌더러가 주입). NEXT_PUBLIC_* 가 아니라 서버에서 읽으므로
 * 이미지 재빌드 없이 재배포만으로 바뀐다 — CHAT_ENABLED 와 같은 패턴.
 *
 * ⚠️ 서버 번들에서만 읽힌다. 클라이언트 컴포넌트에는 layout.tsx 가 prop 으로 넘긴다.
 */
export const DEFAULT_BRAND_NAME = 'AWSome AI Gateway';

/** 사이드바 폭(w-64)에서 레이아웃이 버티는 상한. 넘치면 잘라내고 CSS 말줄임. */
export const MAX_BRAND_NAME_LENGTH = 40;

export function resolveBrandName(raw: string | undefined | null): string {
  const cleaned = (raw ?? '')
    // eslint-disable-next-line no-control-regex
    .replace(/[\u0000-\u001f\u007f]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (!cleaned) return DEFAULT_BRAND_NAME;
  // 코드포인트 단위로 자른다 — 한글·이모지가 반쪽 서로게이트로 깨지지 않게.
  return Array.from(cleaned).slice(0, MAX_BRAND_NAME_LENGTH).join('').trim();
}

export function brandName(): string {
  return resolveBrandName(process.env.ADMIN_BRAND_NAME);
}
