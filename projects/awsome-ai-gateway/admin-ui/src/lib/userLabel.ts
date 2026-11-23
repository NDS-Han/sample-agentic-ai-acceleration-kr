// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 사용자 표시 라벨 — 합성 이메일(`<sub>@unknown`)을 화면에 그대로 내보내지 않는다.
 *
 * 백엔드는 Cognito/OIDC 에서 email_verified 가 아닌 이메일을 DB 에 싣지 않고
 * `<sub>@unknown` 으로 저장한다(ADMIN_EMAILS DB 폴백 사칭 방지 —
 * admin-api cognito_sync_service.py, oidc_service.py). 이 값은 이메일처럼 생겼지만
 * 식별자라 화면에는 display_name 이나 "이메일 미인증" 배지를 대신 보여준다.
 * 저장 규칙은 바꾸지 않는다 — 표시만 다룬다.
 */

const SYNTHETIC_EMAIL_SUFFIX = '@unknown';

/** `<sub>@unknown` 형태의 합성 이메일인지. null/빈 값은 false. */
export function isSyntheticEmail(email: string | null | undefined): boolean {
  return !!email && email.trim().toLowerCase().endsWith(SYNTHETIC_EMAIL_SUFFIX);
}

/** 합성 이메일의 식별자 앞 8자리 + … — 감사 로그 "Unknown user 403b96cd…" 와 같은 표기. */
export function shortSyntheticId(email: string): string {
  const local = email.slice(0, email.length - SYNTHETIC_EMAIL_SUFFIX.length);
  return local.length > 8 ? `${local.slice(0, 8)}…` : local;
}

/**
 * 한 줄 텍스트 라벨(드롭다운 option·차트 라벨 등 배지를 못 넣는 자리용).
 *
 * - 실제 이메일이면 이메일.
 * - 합성 이메일이면 display_name 우선, 없으면 식별자 앞 8자리.
 * - 이메일이 없으면 display_name, 그것도 없으면 빈 문자열.
 *
 * display_name 자체가 합성 이메일인 경우(이름 미설정 계정)는 이름으로 보지 않는다.
 */
export function userLabel({
  email,
  display_name,
}: {
  email?: string | null;
  display_name?: string | null;
}): string {
  const name = display_name?.trim() && !isSyntheticEmail(display_name) ? display_name.trim() : '';
  if (email && !isSyntheticEmail(email)) return email;
  if (name) return name;
  return email ? shortSyntheticId(email) : '';
}
