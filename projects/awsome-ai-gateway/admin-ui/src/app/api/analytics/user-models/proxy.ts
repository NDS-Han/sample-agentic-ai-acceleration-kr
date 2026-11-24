// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { unauthorizedBody } from '@/lib/utils/unauthorized';
import { fetchUpstream, upstreamErrorStatus } from '@/lib/utils/upstream';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

/** 화면이 그대로 읽어야 하는 상류 오류 — 상태와 JSON 본문(error.code)을 보존한다. */
const PASSTHROUGH = new Set([400, 403, 404, 422]);

/** 허용 목록의 파라미터만 상류로 — 임의 키(내부 디버그 플래그 등)를 흘려보내지 않는다. */
export function pickParams(src: URLSearchParams, allowed: readonly string[]): URLSearchParams {
  const out = new URLSearchParams();
  for (const key of allowed) {
    const v = src.get(key);
    if (v !== null && v !== '') out.set(key, v);
  }
  return out;
}

/**
 * user-models 프록시 공통 — analytics-export 와 같은 규칙(admin_jwt 쿠키 전달,
 * 타임아웃, 401 은 기계 판독 JSON). 클라이언트 표가 정렬·페이지를 바꿀 때마다
 * 서버 페이지를 다시 그리지 않도록 표 데이터만 이 경로로 받는다.
 */
export async function proxyUserModels(path: string, params: URLSearchParams): Promise<Response> {
  const jwt = (await cookies()).get('admin_jwt')?.value;
  const qs = params.toString();

  let res: Response;
  try {
    res = await fetchUpstream(`${ADMIN_API_URL}${path}${qs ? `?${qs}` : ''}`, {
      cache: 'no-store',
      headers: { ...(jwt ? { Cookie: `admin_jwt=${jwt}` } : {}) },
    });
  } catch (e) {
    return Response.json(
      { error: { message: 'upstream unreachable' } },
      { status: upstreamErrorStatus(e) },
    );
  }

  if (res.status === 401) return Response.json(unauthorizedBody(), { status: 401 });
  if (!res.ok) {
    if (PASSTHROUGH.has(res.status)) {
      const body = await res.json().catch(() => null);
      return Response.json(body ?? { error: { message: 'request failed' } }, {
        status: res.status,
        headers: { 'Cache-Control': 'no-store' },
      });
    }
    return Response.json({ error: { message: 'upstream error' } }, { status: 502 });
  }
  const data = await res.json();
  return Response.json(data, { headers: { 'Cache-Control': 'no-store' } });
}
