// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 세션 만료 판정 — middleware 의 만료 분기가 실제로 동작하는지.
 *
 * 회귀 배경(FE↔BE 정합성 감사):
 *   * middleware 는 admin_jwt 를 parseJWT 로 풀어 role 만 봤고 `exp` 는 **한 번도**
 *     보지 않았다. 만료된 쿠키로도 페이지가 그대로 열리고, 서버 컴포넌트가 admin-api 를
 *     호출하는 시점에야 전부 401 → 화면은 '—' 와 fetchFailed 로 덮이는데 로그인으로
 *     돌아가는 길이 없었다(쿠키가 '존재'하므로 미인증 분기도 안 탄다).
 *   * 반대 방향 함정: dev-login 이 발급하는 `dev.<payload>.sig` 에는 숫자 exp 클레임이
 *     아예 없다(ISO 문자열 expires_at 만 있다). "exp 없으면 만료"로 구현하면 dev 로그인이
 *     성공한 직후 다시 로그인으로 튕겨 무한 리다이렉트가 된다.
 */

import { describe, it, expect } from 'vitest';
import { isSessionExpired, parseJWT } from '@/lib/auth';
import type { AdminSession } from '@/types/entities';

const NOW = Date.UTC(2026, 8, 9, 12, 0, 0); // 2026-09-09T12:00:00Z

function session(expires_at: string): AdminSession {
  return {
    user_id: 'u1',
    email: 'a@b.c',
    display_name: 'A',
    role: 'ADMIN',
    team_id: null,
    department_id: null,
    issued_at: '',
    expires_at,
  } as AdminSession;
}

function b64url(obj: unknown): string {
  return Buffer.from(JSON.stringify(obj)).toString('base64url');
}

describe('isSessionExpired', () => {
  it('treats a past NumericDate (epoch seconds) as expired', () => {
    const oneHourAgo = Math.floor(NOW / 1000) - 3600;
    expect(isSessionExpired(session(String(oneHourAgo)), NOW)).toBe(true);
  });

  it('treats a future NumericDate as valid', () => {
    const inOneHour = Math.floor(NOW / 1000) + 3600;
    expect(isSessionExpired(session(String(inOneHour)), NOW)).toBe(false);
  });

  it('does NOT expire a token with no exp claim — dev tokens have none', () => {
    expect(isSessionExpired(session(''), NOW)).toBe(false);
    expect(isSessionExpired(session('   '), NOW)).toBe(false);
  });

  it('does not expire on an unparseable exp — 판정 보류', () => {
    expect(isSessionExpired(session('not-a-date'), NOW)).toBe(false);
  });

  it('accepts epoch milliseconds as well as seconds', () => {
    expect(isSessionExpired(session(String(NOW - 3_600_000)), NOW)).toBe(true);
    expect(isSessionExpired(session(String(NOW + 3_600_000)), NOW)).toBe(false);
  });

  it('accepts an ISO 8601 exp', () => {
    expect(isSessionExpired(session(new Date(NOW - 60_000).toISOString()), NOW)).toBe(true);
    expect(isSessionExpired(session(new Date(NOW + 60_000).toISOString()), NOW)).toBe(false);
  });

  it('expires EARLY, not late — admin-api 의 leeway 가 0 이므로 UI 가 먼저 판정해야 한다', () => {
    // ⚠️ 이 테스트는 예전에 반대 방향을 규격으로 못 박고 있었다("exp 를 10초 지났어도 아직
    //    만료 아님"). 그러면 그 구간에서 UI 는 세션이 살아 있다고 보고 페이지를 렌더하는데
    //    admin-api 는 이미 401 을 준다(jose leeway 미지정 = 0,
    //    admin-api/src/app/core/auth.py:63). 결과가 정확히 이 기능이 고치려던 증상이다:
    //    화면은 열리고 모든 값이 '—' 이고 로그인으로 돌아갈 길이 없다.
    //    그래서 UI 는 exp 보다 CLOCK_SKEW_SECONDS 만큼 **먼저** 만료로 본다.

    // exp 가 10초 뒤 = 아직 유효하지만 여유구간 안 → 미리 만료로 보고 로그인으로 보낸다.
    const inTenSeconds = Math.floor(NOW / 1000) + 10;
    expect(
      isSessionExpired(session(String(inTenSeconds)), NOW),
      'exp 직전(10s)인데 유효하다고 보면 admin-api 401 과 어긋나는 창이 생긴다',
    ).toBe(true);

    // exp 를 이미 지났으면 당연히 만료.
    const tenSecondsAgo = Math.floor(NOW / 1000) - 10;
    expect(isSessionExpired(session(String(tenSecondsAgo)), NOW)).toBe(true);

    // 여유구간(30s)을 넘어 충분히 남았으면 만료가 아니다 — 과도하게 튕겨내지 않는다.
    const inTwoMinutes = Math.floor(NOW / 1000) + 120;
    expect(
      isSessionExpired(session(String(inTwoMinutes)), NOW),
      '2분 남은 세션을 만료로 보면 정상 사용자를 튕겨낸다',
    ).toBe(false);
  });
});

describe('isSessionExpired ∘ parseJWT (실제 토큰 모양)', () => {
  it('a dev-login token survives — no exp claim, must not lock dev users out', () => {
    // app/api/auth/dev-login/route.ts 의 buildDevToken 과 동일한 페이로드.
    const token = `dev.${b64url({
      user_id: 'dev-admin',
      email: 'admin@dev.local',
      display_name: 'Dev Admin',
      role: 'ADMIN',
      team_id: null,
      department_id: null,
      issued_at: new Date(NOW).toISOString(),
      expires_at: new Date(NOW + 86_400_000).toISOString(),
    })}.sig`;

    const parsed = parseJWT(token);
    expect(parsed.role).toBe('ADMIN');
    // 숫자 exp 가 없으면 ISO expires_at 로 폴백한다(24h 뒤) — 만료가 아니어야 한다.
    expect(parsed.expires_at).toBe(new Date(NOW + 86_400_000).toISOString());
    expect(isSessionExpired(parsed, NOW)).toBe(false);
  });

  it('an OLD dev-login token IS expired — the ISO expires_at must be honoured', () => {
    // 폴백이 없으면 dev 토큰의 만료가 어디에서도 읽히지 않아 사실상 무기한이 된다.
    const token = `dev.${b64url({
      role: 'ADMIN',
      expires_at: new Date(NOW - 3_600_000).toISOString(),
    })}.sig`;
    expect(isSessionExpired(parseJWT(token), NOW)).toBe(true);
  });

  it('a token with neither exp nor expires_at is not expired', () => {
    const token = `dev.${b64url({ role: 'ADMIN' })}.sig`;
    expect(parseJWT(token).expires_at).toBe('');
    expect(isSessionExpired(parseJWT(token), NOW)).toBe(false);
  });

  it('an expired Cognito-style token (numeric exp) is detected', () => {
    const token = `header.${b64url({
      sub: '11111111-1111-1111-1111-111111111111',
      email: 'user@corp.example',
      role: 'ADMIN',
      iat: Math.floor(NOW / 1000) - 7200,
      exp: Math.floor(NOW / 1000) - 3600,
    })}.sig`;

    expect(isSessionExpired(parseJWT(token), NOW)).toBe(true);
  });

  it('a live Cognito-style token is not expired', () => {
    const token = `header.${b64url({
      sub: '11111111-1111-1111-1111-111111111111',
      role: 'ADMIN',
      exp: Math.floor(NOW / 1000) + 1800,
    })}.sig`;

    expect(isSessionExpired(parseJWT(token), NOW)).toBe(false);
  });
});
