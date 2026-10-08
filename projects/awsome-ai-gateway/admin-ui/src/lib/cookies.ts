// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 세션/임시 쿠키의 `Secure` 플래그 판정 — 로그인·콜백·dev-login·logout 라우트 공용.
 *
 * 기본값은 실제 연결 scheme(`x-forwarded-proto` 첫 토큰): ALB 가 HTTP 종단인 dev
 * 에서 `Secure` 를 강제하면 브라우저가 쿠키를 저장하지 못해 로그인이 무한
 * 리다이렉트로 죽는다(dev-login/route.ts 주석 참조).
 *
 * `SECURE_COOKIES=true` 이면 scheme 과 무관하게 항상 `Secure` 를 붙인다 —
 * TLS 를 프록시 체인 앞단에서 끝내고 `x-forwarded-proto` 를 넘기지 않는
 * 배치, 또는 "내부 HTTP 구간에도 쿠키를 태우지 않겠다" 는 정책용 강제 스위치.
 * 프록시가 proto 를 정상 전달하는 배치에서는 켤 필요 없다.
 */
export function secureCookieFlag(proto: string): boolean {
  return proto === 'https' || (process.env.SECURE_COOKIES ?? '').trim() === 'true';
}
