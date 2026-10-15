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
import { readBoundedBody } from '@/lib/utils/requestBody';
import { upstreamErrorStatus } from '@/lib/utils/upstream';

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

// R17-M2 — 요청 body 상한. 채팅 메시지는 수 KB 수준; JSON+컨텍스트 여유분 64KB.
const MAX_BODY_BYTES = 64 * 1024;
// R17-S6 — upstream fetch 의 연결+응답헤더 타임아웃. 응답이 열리면 해제해야
// SSE 본문 스트림이 계속 흐를 수 있다(AbortSignal.timeout 은 헤더 도착 후에도
// body 를 끊으므로 직접 controller + clearTimeout).
const UPSTREAM_HEADER_TIMEOUT_MS = 15_000;

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
      !seg.includes('/') &&
      // `%3F`/`%23` 디코딩 후 `?`/`#` 가 세그먼트에 남으면 조립된 URL 에서
      // 경로를 절단한다 — 결과가 /admin/chat/ 아래에 머물더라도 호출부가
      // 의도한 경로와 달라지므로 거절 (claude R17 nit).
      !seg.includes('?') &&
      !seg.includes('#'),
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
  // ⚠️ 인증 쿠키 확인을 body 읽기 **앞에** 둔다 (R17-M2). 이 라우트는
  //    middleware 의 /api/ 예외로 비인증 도달이 가능한데, body 를 먼저 버퍼링하면
  //    비인증 대용량 POST 로 파드 메모리를 고갈시킬 수 있다. JWT 없는 요청은
  //    upstream 에 갈 이유도 없으니 즉시 401.
  const jwt = (await cookies()).get('admin_jwt')?.value;
  if (!jwt) {
    return Response.json(unauthorizedBody(), { status: 401 });
  }
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
  headers.Cookie = `admin_jwt=${jwt}`;

  const init: RequestInit = {
    method: req.method,
    headers,
    cache: 'no-store',
    // @ts-expect-error — Node fetch 에서 streaming 요청 body 처리용
    duplex: 'half',
  };
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    // R17-M2 — 상한 있는 읽기(req.text() 는 무제한 버퍼링). Content-Length 로
    // 조기 거절 + chunked 는 읽으며 계수.
    const body = await readBoundedBody(req, MAX_BODY_BYTES);
    if (!body.ok) return body.response;
    init.body = body.text;
  }

  // R17-S6 — admin-api 장애/지연 시 라우트가 무기한 대기하지 않게 헤더 도착까지
  // 타임아웃을 건다. 응답이 열리면 즉시 해제해 SSE 본문은 끝까지 흐른다.
  // upstream 연결 자체가 실패하면 빈 500 이 아니라 502 로.
  const upstreamAbort = new AbortController();
  const headerTimer = setTimeout(
    () => upstreamAbort.abort(),
    UPSTREAM_HEADER_TIMEOUT_MS,
  );
  let upstream: Response;
  try {
    upstream = await fetch(target, { ...init, signal: upstreamAbort.signal });
  } catch (e) {
    return Response.json(
      { error: 'upstream unreachable' },
      { status: upstreamErrorStatus(e) },
    );
  } finally {
    clearTimeout(headerTimer);
  }

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

export async function GET(req: NextRequest, props: { params: Promise<{ path: string[] }> }) {
  const params = await props.params;
  return forward(req, params.path);
}

export async function POST(req: NextRequest, props: { params: Promise<{ path: string[] }> }) {
  const params = await props.params;
  return forward(req, params.path);
}
