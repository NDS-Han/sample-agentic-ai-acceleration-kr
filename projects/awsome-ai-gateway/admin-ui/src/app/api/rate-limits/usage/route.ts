// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// 실시간 RPM 사용량(§60.9) 클라이언트 폴링용 프록시 — /users ScopeRateLimitPanel 이
// 10초마다 호출. admin-api /admin/rate-limits/usage/{scope}/{scope_id} 로 전달.

import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';
import { unauthorizedBody } from '@/lib/utils/unauthorized';
import { fetchUpstream } from '@/lib/utils/upstream';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

// R25-N-C11 — scope/scope_id 는 URL 세그먼트로 합쳐진다. encodeURIComponent('..')
// 는 '..' 그대로라 `/usage/../x` 가 경로 정규화로 탈출한다 — cli-download 와 같은
// 문자셋 화이트리스트로 먼저 거른다(scope 는 user|team|global, id 는 UUID 라
// 영숫자+`-`/`_` 면 충분하다).
const SAFE_SEGMENT = /^[a-z0-9_-]+$/i;

export async function GET(req: NextRequest) {
  const jwt = (await cookies()).get('admin_jwt')?.value;
  const sp = req.nextUrl.searchParams;
  const scope = sp.get('scope') ?? '';
  const scopeId = sp.get('scope_id') ?? '';
  if (!scope || !scopeId) {
    return NextResponse.json({ available: false, reason: 'missing params' }, { status: 400 });
  }
  if (!SAFE_SEGMENT.test(scope) || !SAFE_SEGMENT.test(scopeId)) {
    return NextResponse.json({ available: false, reason: 'invalid params' }, { status: 400 });
  }

  const upstream = `${ADMIN_API_URL}/admin/rate-limits/usage/${encodeURIComponent(scope)}/${encodeURIComponent(scopeId)}`;
  try {
    const res = await fetchUpstream(upstream, {
      cache: 'no-store',
      headers: {
        'Content-Type': 'application/json',
        ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
      },
    });
    // ⚠️ 401 만 fail-soft 200 에서 빼낸다. 나머지(403/5xx)는 그대로 fail-soft 다 —
    //    RL 설정 화면이 실시간 카운터 하나 때문에 막히면 안 된다. 다만 401 은 세션이
    //    죽었다는 뜻이라 "실시간 조회 불가" 로 뭉개면 사용자가 10초마다 조용히 실패하는
    //    폴링을 영원히 돌린다.
    //    available:false 를 함께 남기는 이유: 기존 소비자
    //    (src/lib/utils/rateLimitUsage.ts)는 res.ok 를 보지 않고 본문만 읽으므로,
    //    필드를 빼면 그쪽이 undefined 를 만나 조용히 형태가 달라진다.
    if (res.status === 401) {
      return NextResponse.json(
        { ...unauthorizedBody(), available: false, reason: 'unauthorized' },
        { status: 401 },
      );
    }
    if (!res.ok) {
      return NextResponse.json({ available: false, reason: `upstream ${res.status}` }, { status: 200 });
    }
    return NextResponse.json(await res.json());
  } catch {
    // fail-soft: 실시간 조회 실패가 RL 설정 화면을 막지 않게 200 + available:false.
    return NextResponse.json({ available: false, reason: 'fetch error' }, { status: 200 });
  }
}
