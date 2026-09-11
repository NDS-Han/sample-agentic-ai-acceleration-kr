// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { AdminSession } from '@/types/entities';
import type { UserRole } from '@/types/enums';
import { PAGE_PERMISSIONS } from './permissions';

/**
 * Decodes a JWT without verifying the signature.
 *
 * Middleware only checks cookie presence; signature verification is handled
 * server-side by the API. This utility is used client-side / in RSC to read
 * the payload for display and permission gating.
 *
 * @throws {Error} When the token is malformed or the payload cannot be parsed.
 */
export function parseJWT(token: string): AdminSession {
  const parts = token.split('.');

  if (parts.length !== 3) {
    throw new Error('Invalid JWT format: expected 3 dot-separated segments');
  }

  const payloadSegment = parts[1];

  // Base64URL → Base64 → JSON
  const base64 = payloadSegment.replace(/-/g, '+').replace(/_/g, '/');
  const padded = base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), '=');

  let jsonString: string;
  try {
    jsonString = atob(padded);
  } catch {
    throw new Error('Failed to decode JWT payload: invalid base64');
  }

  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(jsonString) as Record<string, unknown>;
  } catch {
    throw new Error('Failed to parse JWT payload: invalid JSON');
  }

  return {
    user_id: String(payload['sub'] ?? payload['user_id'] ?? ''),
    email: String(payload['email'] ?? ''),
    display_name: String(payload['display_name'] ?? payload['name'] ?? ''),
    role: payload['role'] as UserRole,
    team_id: (payload['team_id'] as string | null) ?? null,
    department_id: (payload['department_id'] as string | null) ?? null,
    issued_at: String(payload['iat'] ?? payload['issued_at'] ?? ''),
    // exp(표준 NumericDate) 우선, 없으면 expires_at(ISO) — dev-login 토큰은 후자만 갖는다.
    // 폴백이 없으면 dev 토큰의 24h 만료가 어디에서도 읽히지 않아 사실상 무기한이 된다.
    expires_at: String(payload['exp'] ?? payload['expires_at'] ?? ''),
  };
}

/**
 * 시계 오차 여유(초). UI 는 admin-api 보다 **먼저** 만료로 판정한다.
 *
 * ⚠️ 방향이 중요하다. admin-api 의 jose `jwt.decode` 는 leeway 를 넘기지 않아 기본값 0 이다
 *    (admin-api/src/app/core/auth.py:63, core/oidc_verifier.py:233) — 즉 exp 시점에 정확히
 *    거절한다. 그러므로 UI 가 exp 보다 **늦게** 만료로 보면(+skew) 그 구간에서 페이지는
 *    열리는데 서버 컴포넌트의 모든 호출이 401 이 되어, 고치려던 바로 그 증상('—' 와
 *    fetchFailed 만 깔린 화면)이 재현된다. 그래서 빼는 것이 맞다.
 *
 *    반대 방향의 대가는 작다: 아직 유효한 토큰을 최대 30초 일찍 로그인으로 보내는 것뿐이고,
 *    실제 서명·만료 판정은 admin-api 가 다시 하므로 여기서의 보수적 판정은 권한 문제가 없다.
 */
const CLOCK_SKEW_SECONDS = 30;

/**
 * 세션이 만료됐는지. **만료를 확신할 수 없으면 false**(=만료 아님) 를 돌려준다.
 *
 * ⚠️ 이 관용이 핵심이다. dev-login 이 발급하는 토큰(`dev.<payload>.sig`)에는 숫자
 *    `exp` 클레임이 아예 없고 ISO 문자열 `expires_at` 만 있어서, parseJWT 를 통과하면
 *    session.expires_at 이 빈 문자열이 된다. 여기서 "값이 없으면 만료"로 판정하면
 *    dev 사용자가 로그인하는 순간 다시 로그인 화면으로 튕겨 무한 리다이렉트가 된다.
 *
 * 배경: 예전엔 만료 검사 자체가 없어서, 만료된 admin_jwt 로도 페이지가 그대로 열리고
 * 서버 컴포넌트의 모든 API 호출만 401 이 됐다 — 화면에는 '—' 와 fetchFailed 만 깔리고
 * 로그인으로 돌아갈 길이 없었다.
 */
export function isSessionExpired(session: AdminSession, nowMs: number = Date.now()): boolean {
  const raw = session.expires_at?.trim();
  if (!raw) return false; // exp 클레임 없음(dev 토큰) → 만료로 취급하지 않는다

  // JWT 표준: NumericDate = epoch **초**. 밀리초로 넣는 발급자도 있어 자릿수로 구분한다.
  let expiresAtMs: number;
  if (/^\d+$/.test(raw)) {
    const n = Number(raw);
    expiresAtMs = n > 1e11 ? n : n * 1000;
  } else {
    expiresAtMs = Date.parse(raw); // ISO 8601 도 받아준다
  }

  if (!Number.isFinite(expiresAtMs)) return false; // 해석 불가 → 판정 보류

  return expiresAtMs - CLOCK_SKEW_SECONDS * 1000 <= nowMs;
}

/**
 * Checks whether `role` is allowed to access the given `pathname`.
 *
 * Uses prefix matching: `/budgets/123` is covered by the `/budgets` entry.
 * Falls back to `false` (deny) for paths not listed in PAGE_PERMISSIONS.
 */
export function checkPagePermission(pathname: string, role: UserRole): boolean {
  // Find the most specific (longest) matching prefix
  const matchingEntry = Object.entries(PAGE_PERMISSIONS)
    .filter(([prefix]) => {
      if (prefix === '/') {
        // Root matches only the exact path '/' to avoid swallowing everything
        return pathname === '/';
      }
      return pathname === prefix || pathname.startsWith(`${prefix}/`);
    })
    .sort(([a], [b]) => b.length - a.length)[0];

  if (!matchingEntry) {
    return false; // No permission entry found — deny by default
  }

  const [, allowedRoles] = matchingEntry;
  return allowedRoles.includes(role);
}
