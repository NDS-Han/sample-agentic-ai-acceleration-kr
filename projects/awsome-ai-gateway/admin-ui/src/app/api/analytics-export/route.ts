// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';
import { unauthorizedBody } from '@/lib/utils/unauthorized';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET(request: NextRequest) {
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;

  const searchParams = request.nextUrl.searchParams.toString();

  const res = await fetch(`${ADMIN_API_URL}/admin/analytics/export?${searchParams}`, {
    cache: 'no-store',
    headers: {
      ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
    },
  });

  // ⚠️ 401 에 'Export failed' 평문을 주면 ExportButton 은 다운로드가 왜 안 되는지 알 수
  //    없다(현재 구현은 console.error 만 남기고 화면엔 아무 변화가 없다). 기계 판독용
  //    JSON 으로 바꿔 로그인 유도를 가능하게 한다.
  if (res.status === 401) {
    return Response.json(unauthorizedBody(), { status: 401 });
  }
  if (!res.ok) {
    return new Response('Export failed', { status: res.status });
  }

  const contentType = res.headers.get('content-type') || 'application/octet-stream';
  const contentDisposition = res.headers.get('content-disposition') || 'attachment';
  const body = await res.blob();

  return new Response(body, {
    headers: {
      'Content-Type': contentType,
      'Content-Disposition': contentDisposition,
    },
  });
}
