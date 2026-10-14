// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * readBoundedBody 회귀 (R17-M2).
 *
 * 배경: /api/* 라우트는 middleware 가 인증 검사 없이 통과시키고, route handler 가
 * req.text()/json() 으로 body 를 무제한 버퍼링했다 — 비인증 대용량 POST 몇 개로
 * 파드 OOM. readBoundedBody 는 Content-Length 선차단 + 스트리밍 누적 상한을 둔다.
 */

import { describe, expect, it } from 'vitest';
import { NextRequest } from 'next/server';
import { readBoundedBody } from '@/lib/utils/requestBody';

function req(body: BodyInit | null, headers?: Record<string, string>): NextRequest {
  return new NextRequest('http://admin.test/api/x', {
    method: 'POST',
    body,
    headers,
  });
}

describe('readBoundedBody', () => {
  it('상한 이내 body 는 그대로 돌려준다', async () => {
    const r = await readBoundedBody(req('{"a":1}'), 1024);
    expect(r).toEqual({ ok: true, text: '{"a":1}' });
  });

  it('Content-Length 가 상한 초과면 읽지 않고 413', async () => {
    // body 스트림을 만들되 Content-Length 만 선언 — 헤더만으로 차단돼야 한다.
    const r = await readBoundedBody(
      req('x', { 'content-length': String(10 * 1024 * 1024) }),
      1024
    );
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.response.status).toBe(413);
  });

  it('chunked(헤더 없음)로 흘려도 누적 바이트가 상한을 넘으면 413', async () => {
    // ReadableStream body — Content-Length 헤더가 없어 스트리밍 카운트 경로를 탄다.
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(new Uint8Array(2048)); // 2KB in one chunk
        c.close();
      },
    });
    const r = await readBoundedBody(req(stream), 1024);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.response.status).toBe(413);
  });

  it('멀티바이트 문자가 청크 경계에 걸쳐도 깨지지 않는다', async () => {
    // TextDecoder {stream:true} 경로 — 1바이트씩 쪼개 보낸다.
    const bytes = new TextEncoder().encode('{"msg":"한"}');
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        for (const b of bytes) c.enqueue(new Uint8Array([b]));
        c.close();
      },
    });
    const r = await readBoundedBody(req(stream), 1024);
    expect(r.ok).toBe(true);
    if (r.ok) expect(r.text).toBe('{"msg":"한"}');
  });

  it('body 없는 요청은 빈 문자열', async () => {
    const r = await readBoundedBody(req(null), 1024);
    expect(r).toEqual({ ok: true, text: '' });
  });
});
