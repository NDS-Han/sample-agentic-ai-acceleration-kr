// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { NextRequest } from 'next/server';
import { cookies } from 'next/headers';
import { readBoundedBody } from '@/lib/utils/requestBody';
import { unauthorizedBody } from '@/lib/utils/unauthorized';
import { parseJWT, isSessionExpired } from '@/lib/auth';

// R17-M2 — 비인증 도달 가능한 로그 수신 endpoint. 인증 확인을 body 파싱 앞에 두고
// 크기 상한을 둔다. 상한 없는 request.json() 은 비인증 메모리/로그폭주 DoS 였다.
const MAX_BODY_BYTES = 8 * 1024;
// 필드별 문자열 상한 — 스택트레이스도 수 KB 면 충분.
const MAX_FIELD_CHARS = 2048;
// 허용 필드만 통과 — 임의 키를 그대로 로그에 넣으면 level/source 같은 라벨을
// 덮어쓰거나(spread 순서) 구조화 로그 파서를 오염시킨다(로그 위조).
const ALLOWED_FIELDS = new Set(['message', 'stack', 'url', 'component', 'digest']);

// 위조 JWT 통과 잔여면: 이 라우트는 admin-api 를 호출하지 않으므로 "서명은
// upstream 이 검증" 전제가 성립하지 않는다 — 형식+만료만 맞는 위조 쿠키로
// 무제한 로그 쓰기가 가능했다. per-IP fixed-window 로 상한을 둔다.
const WINDOW_MS = 60_000;
const MAX_PER_WINDOW = 20;
const _buckets = new Map<string, { start: number; count: number }>();

function rateLimited(ip: string): boolean {
  const now = Date.now();
  const b = _buckets.get(ip);
  if (!b || now - b.start >= WINDOW_MS) {
    _buckets.set(ip, { start: now, count: 1 });
    // 무한 성장 방지 — 창이 지난 항목을 주기적으로 청소한다.
    if (_buckets.size > 10_000) {
      for (const [k, v] of _buckets) if (now - v.start >= WINDOW_MS) _buckets.delete(k);
    }
    return false;
  }
  b.count += 1;
  return b.count > MAX_PER_WINDOW;
}

function truncate(v: unknown): string | undefined {
  if (v === undefined || v === null) return undefined;
  const s = typeof v === 'string' ? v : String(v);
  return s.slice(0, MAX_FIELD_CHARS);
}

export async function POST(request: NextRequest) {
  // 인증 없는 에러 보고는 신뢰 가치가 없다 — 읽기 전에 거절. 쿠키 존재만 보면
  // `admin_jwt=x` 한 쌍으로 로그를 무한히 찍을 수 있으니(로그 폭주 위조) JWT
  // 파싱 + 만료까지 검증한다. 서명 검증은 없지만 admin-api 가 최종적으로 서명을
  // 검증하므로 여기서는 형식/만료 필터면 충분하다 (kiro R17 #12).
  const jwt = (await cookies()).get('admin_jwt')?.value;
  if (!jwt) {
    return Response.json(unauthorizedBody(), { status: 401 });
  }
  try {
    const session = parseJWT(jwt);
    if (isSessionExpired(session)) {
      return Response.json(unauthorizedBody(), { status: 401 });
    }
  } catch {
    return Response.json(unauthorizedBody(), { status: 401 });
  }

  // XFF 가장 오른쪽 홉 = 프록시가 기록한 직접 클라이언트(위조 불가 영역).
  const xff = request.headers.get('x-forwarded-for') ?? '';
  const clientIp = xff.split(',').pop()?.trim() || 'unknown';
  if (rateLimited(clientIp)) {
    return Response.json({ error: 'rate_limited' }, { status: 429 });
  }

  const bounded = await readBoundedBody(request, MAX_BODY_BYTES);
  if (!bounded.ok) return bounded.response;

  let parsed: unknown = {};
  try {
    parsed = JSON.parse(bounded.text || '{}');
  } catch {
    // Malformed JSON — log what we have
  }

  const src =
    typeof parsed === 'object' && parsed !== null
      ? (parsed as Record<string, unknown>)
      : {};

  const entry: Record<string, string> = {};
  for (const key of ALLOWED_FIELDS) {
    const v = truncate(src[key]);
    if (v) entry[key] = v;
  }

  console.error(
    JSON.stringify({
      ...entry,
      level: 'error',
      source: 'client',
      timestamp: new Date().toISOString(),
    })
  );

  return new Response(null, { status: 204 });
}
