// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { NextRequest, NextResponse } from 'next/server';
import { unauthorizedBody } from '@/lib/utils/unauthorized';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET(req: NextRequest) {
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;

  const sp = req.nextUrl.searchParams;
  const period = sp.get('period') ?? '';

  const upstream = new URL(`${ADMIN_API_URL}/admin/dashboard/client-share`);
  if (period) upstream.searchParams.set('period', period);

  const res = await fetch(upstream.toString(), {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
    },
  });

  // ⚠️ 401 은 상태코드만 되돌려주면 소비자가 "HTTP 401" 문자열밖에 만들 수 없다.
  //    error_code 를 실어 로그인 유도가 가능하게 한다(model-share 와 동일 계약).
  if (res.status === 401) {
    return NextResponse.json(unauthorizedBody(), { status: 401 });
  }
  if (!res.ok) {
    return NextResponse.json({ error: '앱별 비용 점유율 조회 실패' }, { status: res.status });
  }
  const data = await res.json();
  return NextResponse.json(data);
}
