// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';
import { unauthorizedBody } from '@/lib/utils/unauthorized';
import { fetchUpstream, upstreamErrorStatus } from '@/lib/utils/upstream';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

export async function GET(request: NextRequest) {
  const cookieStore = await cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;

  const searchParams = request.nextUrl.searchParams.toString();

  // R17-S6 — upstream 연결 실패/타임아웃은 프레임워크 500 이 아니라 502 로.
  let res: Response;
  try {
    res = await fetchUpstream(`${ADMIN_API_URL}/admin/analytics/export?${searchParams}`, {
      cache: 'no-store',
      headers: {
        ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}),
      },
    });
  } catch (e) {
    return new Response('Export failed (upstream unreachable)', { status: upstreamErrorStatus(e) });
  }

  // ⚠️ 401 에 'Export failed' 평문을 주면 ExportButton 은 다운로드가 왜 안 되는지 알 수
  //    없다(현재 구현은 console.error 만 남기고 화면엔 아무 변화가 없다). 기계 판독용
  //    JSON 으로 바꿔 로그인 유도를 가능하게 한다.
  if (res.status === 401) {
    return Response.json(unauthorizedBody(), { status: 401 });
  }
  if (!res.ok) {
    return new Response('Export failed', { status: res.status });
  }

  // 상류 헤더의 파일명을 그대로 되돌려주지 않는다 — 따옴표·개행이 섞이면
  // Content-Disposition 이 깨진다. cli-download 와 같은 화이트리스트 살균.
  const upstreamName = res.headers
    .get('content-disposition')
    ?.match(/filename="?([^";]+)"?/)?.[1]
    ?.replace(/[^A-Za-z0-9._-]/g, '');
  const filename = upstreamName && upstreamName.length > 0
    ? upstreamName
    : 'analytics-export.csv';

  const headers = new Headers({
    'Content-Type': res.headers.get('content-type') || 'application/octet-stream',
    'Content-Disposition': `attachment; filename="${filename}"`,
    'Cache-Control': 'no-store',
  });
  const length = res.headers.get('content-length');
  if (length) headers.set('Content-Length', length);

  // res.blob() 으로 전체를 메모리에 올리지 않는다 — export 가 클수록 admin-ui 파드
  // 메모리를 그대로 먹는다(cli-download 와 같은 이유로 스트림 통과).
  return new Response(res.body, { status: 200, headers });
}
