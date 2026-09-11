// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { NextResponse } from 'next/server';
import { unauthorizedBody } from '@/lib/utils/unauthorized';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET() {
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;

  const res = await fetch(`${ADMIN_API_URL}/admin/users/teams`, {
    cache: 'no-store',
    headers: {
      'Content-Type': 'application/json',
      ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
    },
  });

  // ⚠️ 401 은 error_code 로 구분해 준다 — 소비자가 '조회 실패' 문구를 렌더하는 대신
  //    로그인으로 갈 수 있어야 한다. 403(권한 부족)은 여기 들어오지 않는다.
  if (res.status === 401) {
    return NextResponse.json(unauthorizedBody(), { status: 401 });
  }
  if (!res.ok) {
    return NextResponse.json({ error: '팀 목록 조회 실패' }, { status: res.status });
  }

  const data = (await res.json()) as { items?: Array<{ id: string; name: string }> };
  const teams = (data.items ?? []).map((t) => ({ id: t.id, name: t.name }));
  return NextResponse.json(teams);
}
