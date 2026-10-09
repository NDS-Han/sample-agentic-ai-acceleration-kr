// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * admin-chat-agent server-side proxy.
 *
 * chat 컴포넌트(client)는 브라우저에서 직접 admin-api 에 닿을 수 없으므로
 * (CORS / 내부 DNS / 인증 쿠키), 기존 teams-proxy 와 동일하게 admin-ui 의
 * server route 가 admin_jwt 쿠키를 admin-api 로 전달한다.
 *
 * 다른 proxy route 와 달리 chat 의 messages 엔드포인트는 SSE(text/event-stream)
 * 스트림이라, 응답 body(ReadableStream)를 버퍼링 없이 그대로 흘려보낸다.
 *
 * 경로 매핑: /api/chat-proxy/admin/chat/...  →  $ADMIN_API_URL/admin/chat/...
 */

import { cookies } from 'next/headers';
import { NextRequest } from 'next/server';
import { unauthorizedBody } from '@/lib/utils/unauthorized';

// SSE pass-through 라우트 — Next.js 가 응답을 버퍼링/정적최적화하지 않도록 강제.
// 이 설정이 없으면 standalone 런타임이 ReadableStream(특히 admin-api 의 10초
// `: keepalive` 코멘트)을 즉시 flush 하지 않아, AgentCore 버퍼링으로 인한 긴 침묵
// 구간(case02: sub-agent ~59초)에서 브라우저↔서버 연결이 idle 로 끊긴다(§51).
//   - force-dynamic: 라우트를 매 요청 동적 실행(정적/캐시 최적화 비활성)
//   - runtime nodejs: edge 가 아닌 node 스트리밍(ReadableStream 즉시 전달)
//   - fetchCache no-store: upstream fetch 결과 캐시/버퍼 금지
//   - maxDuration: 리포트 등 장시간(최대 ~300s) 스트림 중 라우트 강제종료 방지
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';
export const fetchCache = 'force-no-store';
export const maxDuration = 300;

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

/**
 * 경로 화이트리스트 (R3-2). 이 라우트는 `/api/chat-proxy/admin/chat/...` 만
 * 서비스해야 한다. catch-all 세그먼트를 그대로 join 하면 `..`/`%2e%2e` 가
 * URL 정규화로 상위 디렉터리를 빠져나가 — 라이브로
 * `/api/chat-proxy/admin/chat/%2e%2e/%2e%2e/internal/cache/retry` →
 * admin-api `/internal/cache/retry` 도달이 확인됐다. 사용자의 admin_jwt 가
 * 임의 admin-api 경로로 전달되는 것을 차단한다.
 */
function isAllowedPath(pathParts: string[]): boolean {
  if (pathParts.length < 3) return false;
  if (pathParts[0] !== 'admin' || pathParts[1] !== 'chat') return false;
  // 세그먼트 디코딩 후에도 위험 문자가 남으면 거절 — `..`, `.`, 빈 세그먼트,
  // 이중 인코딩(`%`), 백슬래시.
  // R4: 슬래시도 거절 — Next.js 는 `%2f` 를 세그먼트 "내부"의 `/` 로
  // 디코딩하므로(예: `..%2finternal` → 세그먼트 `../internal`), 세그먼트 안
  // 슬래시는 join('/') 이 되살리면서 실제 경로 구분자로 부활한다.
  return pathParts.every(
    (seg) =>
      seg !== '' &&
      seg !== '.' &&
      seg !== '..' &&
      !seg.includes('%') &&
      !seg.includes('\\') &&
      !seg.includes('/'),
  );
}

/**
 * 조립된 upstream URL 을 정규화한 뒤 `/admin/chat/` 아래인지 재확인(R4
 * 심층방어). 세그먼트 검사가 우회되더라도 URL 의 `..` 정규화로 탈출한
 * 경로는 여기서 걸린다. 실패하면 null → 400.
 */
function buildTargetUrl(pathParts: string[], search: string): URL | null {
  try {
    const url = new URL(
      `${ADMIN_API_URL}/${pathParts.join('/')}${search}`,
    );
    if (!url.pathname.startsWith('/admin/chat/')) return null;
    return url;
  } catch {
    return null;
  }
}

async function forward(req: NextRequest, pathParts: string[]): Promise<Response> {
  if (!isAllowedPath(pathParts)) {
    return Response.json(
      { error: 'chat-proxy path not allowed' },
      { status: 400 },
    );
  }
  const jwt = cookies().get('admin_jwt')?.value;
  const search = req.nextUrl.search || '';
  const targetUrl = buildTargetUrl(pathParts, search);
  if (!targetUrl) {
    return Response.json(
      { error: 'chat-proxy path not allowed' },
      { status: 400 },
    );
  }
  const target = targetUrl.toString();

  const headers: Record<string, string> = {
    'Content-Type': req.headers.get('content-type') || 'application/json',
    Accept: req.headers.get('accept') || 'application/json',
  };
  if (jwt) headers.Cookie = `admin_jwt=${jwt}`;

  const init: RequestInit = {
    method: req.method,
    headers,
    cache: 'no-store',
    // @ts-expect-error — Node fetch 에서 streaming 요청 body 처리용
    duplex: 'half',
  };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    init.body = await req.text();
  }

  const upstream = await fetch(target, init);

  // ⚠️ pass-through 의 유일한 예외: 401. 인증 실패 응답은 SSE 스트림이 아니라 JSON 한
  //    덩이라서 흘려보낼 이유가 없고, 그대로 흘리면 useChatStream 이 `HTTP 401` 문자열만
  //    onError 로 던져(components/chat/useChatStream.ts) 사용자는 이유 없이 멈춘 채팅을
  //    본다. 기계 판독용 본문으로 바꿔 로그인 유도를 가능하게 한다.
  //    상류 body 는 읽지 않고 버리므로 명시적으로 취소한다(미소비 스트림 경고 방지).
  if (upstream.status === 401) {
    upstream.body?.cancel().catch(() => {});
    return Response.json(unauthorizedBody(), { status: 401 });
  }

  // SSE 등 스트리밍 응답은 body 를 그대로 pass-through (버퍼링 금지)
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      'Content-Type':
        upstream.headers.get('content-type') || 'application/json',
      'Cache-Control': 'no-cache, no-transform',
      'X-Accel-Buffering': 'no',
    },
  });
}

export async function GET(
  req: NextRequest,
  { params }: { params: { path: string[] } }
) {
  return forward(req, params.path);
}

export async function POST(
  req: NextRequest,
  { params }: { params: { path: string[] } }
) {
  return forward(req, params.path);
}
