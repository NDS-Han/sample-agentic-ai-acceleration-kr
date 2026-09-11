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
  const teamId = sp.get('team_id') ?? 'all';
  const client = sp.get('client') ?? '';

  const upstream = new URL(`${ADMIN_API_URL}/admin/dashboard/model-share`);
  if (period) upstream.searchParams.set('period', period);
  upstream.searchParams.set('team_id', teamId);
  if (client && client !== 'all') upstream.searchParams.set('client', client);

  const res = await fetch(upstream.toString(), {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
    },
  });

  // ⚠️ 401 은 `!res.ok` 로 뭉개면 안 된다. middleware 는 `/api/` 전체를 공개 경로로
  //    통과시키므로 이 라우트에는 만료 검사가 없고, 여기가 세션 사망을 처음 알게 되는
  //    지점이다. 기계 판독용 error_code 로 내려보내야 ModelShareDonutClient 가
  //    "조회 실패: HTTP 401" 을 찍는 대신 로그인으로 보낼 수 있다.
  if (res.status === 401) {
    return NextResponse.json(unauthorizedBody(), { status: 401 });
  }
  if (!res.ok) {
    return NextResponse.json(
      { error: '모델별 비용 점유율 조회 실패' },
      { status: res.status },
    );
  }
  const data = await res.json();
  return NextResponse.json(data);
}
