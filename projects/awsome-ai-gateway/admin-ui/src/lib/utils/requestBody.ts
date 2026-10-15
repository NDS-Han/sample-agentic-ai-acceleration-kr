// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { NextRequest, NextResponse } from 'next/server';

/**
 * 요청 body 를 바이트 상한 안에서 읽는다.
 *
 * App Router route handler 에는 기본 body 크기 제한이 없다 — `req.text()` /
 * `request.json()` 은 body 전체를 메모리에 올린다. `/api/*` 는 middleware 에서
 * 인증 검사 없이 통과하므로, 인증 쿠키 확인 **전에** 무제한 버퍼링을 하면
 * 비인증 대용량 POST 몇 개로 파드가 OOM 난다(R17-M2 실측).
 *
 * 읽기 순서:
 *   1. Content-Length 헤더가 상한 초과면 읽지 않고 413 — 우리가 버퍼링하지
 *      않는다(런타임이 잔여 본문을 읽어 버리기는 한다 — 애플리케이션 메모리
 *      상한과는 무관).
 *   2. 헤더가 없거나 거짓이면(chunked) req.body 를 청크로 읽으며 누적 바이트가
 *      상한을 넘는 순간 스트림을 cancel 하고 413.
 *
 * 반환: { ok: true, text } | { ok: false, response } — 실패 시 response 를
 * 그대로 return 하면 된다.
 */
export async function readBoundedBody(
  req: NextRequest,
  limit: number,
): Promise<{ ok: true; text: string } | { ok: false; response: NextResponse }> {
  const tooLarge = () =>
    NextResponse.json(
      { error: 'request body too large' },
      { status: 413 },
    );

  const declared = Number(req.headers.get('content-length') ?? '');
  if (Number.isFinite(declared) && declared > limit) {
    // 소비되지 않은 body 는 연결을 막을 수 있으나, 413 즉시 반환이
    // Next/Node 에서 소켓을 정리한다. 읽지 않는 게 포인트.
    return { ok: false, response: tooLarge() };
  }

  const body = req.body;
  if (!body) {
    return { ok: true, text: '' };
  }

  const reader = body.getReader();
  const decoder = new TextDecoder();
  let received = 0;
  const chunks: string[] = [];
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
      if (received > limit) {
        await reader.cancel().catch(() => {});
        return { ok: false, response: tooLarge() };
      }
      chunks.push(decoder.decode(value, { stream: true }));
    }
    chunks.push(decoder.decode());
  } finally {
    reader.releaseLock();
  }
  return { ok: true, text: chunks.join('') };
}
