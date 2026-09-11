// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * CLI 다운로드 프록시 라우트 핸들러를 **실제로 실행**한다.
 *
 * 왜 별도 파일인가: navigation.test.ts 는 소스 텍스트만 본다(경로를 다시 쓰는지, 중계
 * 라우트가 하나인지). 그것만으로는 핸들러가 무엇을 하는지 전혀 증명하지 못한다 —
 * 스트리밍인지, 상류 404 를 그대로 전달하는지, 상류 헤더의 파일명을 그대로 헤더에
 * 되박는지는 실행해야만 드러난다.
 *
 * 상류(admin-api)는 fetch 를 스텁해서 흉내낸다. 스텁 객체의 blob() 은 호출되면 던지도록
 * 해 두었다 — 예전 구현이 res.blob() 으로 패키지 전체를 메모리에 올렸기 때문에,
 * 그 회귀가 돌아오면 이 테스트가 바로 깨진다(구조적 음성대조군).
 */

// @vitest-environment node

import { describe, it, expect, vi, afterEach } from 'vitest';
import { NextRequest } from 'next/server';
import { GET } from '@/app/api/cli-download/[os]/[arch]/route';

function streamOf(...chunks: string[]): ReadableStream<Uint8Array> {
  const enc = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const c of chunks) controller.enqueue(enc.encode(c));
      controller.close();
    },
  });
}

/** 상류 응답 흉내 — 버퍼링 계열 메서드는 호출 자체가 실패다. */
function upstream(opts: {
  status?: number;
  headers?: Record<string, string>;
  body?: ReadableStream<Uint8Array> | null;
}) {
  const status = opts.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: new Headers(opts.headers ?? {}),
    body: opts.body ?? null,
    blob: () => {
      throw new Error('버퍼링 회귀: res.blob() 이 호출됐다');
    },
    arrayBuffer: () => {
      throw new Error('버퍼링 회귀: res.arrayBuffer() 가 호출됐다');
    },
    text: () => {
      throw new Error('버퍼링 회귀: res.text() 가 호출됐다');
    },
  };
}

const req = () => new NextRequest('http://admin.test/api/cli-download/linux/amd64');

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('GET /api/cli-download/[os]/[arch]', () => {
  it('상류 본문을 그대로 흘려보낸다(버퍼링하지 않는다)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        upstream({
          headers: {
            'content-type': 'application/gzip',
            'content-disposition': 'attachment; filename="gateway-cli-0.1.0-linux-amd64.tar.gz"',
            'content-length': '9',
          },
          body: streamOf('tarball!!'),
        }),
      ),
    );

    const res = await GET(req(), { params: { os: 'linux', arch: 'amd64' } });

    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/gzip');
    expect(res.headers.get('content-disposition')).toBe(
      'attachment; filename="gateway-cli-0.1.0-linux-amd64.tar.gz"',
    );
    // 브라우저 진행률 표시를 위해 길이를 전달한다.
    expect(res.headers.get('content-length')).toBe('9');
    expect(res.headers.get('cache-control')).toBe('no-store');
    expect(await res.text()).toBe('tarball!!');
  });

  it('상류 404(패키지 미빌드)를 502 로 뭉개지 않고 그대로 전달한다', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => upstream({ status: 404 })));

    const res = await GET(req(), { params: { os: 'linux', arch: 'amd64' } });

    // 이 구분이 실제 운영에서 중요했다: 버튼이 404 였던 원인은 admin-ui 가 아니라
    // admin-api 의 CLI_DIST_DIR 이 비어 있던 것이다. 502 로 덮으면 그 진단이 불가능해진다.
    expect(res.status).toBe(404);
    expect(await res.text()).toContain('upstream 404');
  });

  it('상류 5xx 도 상태코드를 보존한다', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => upstream({ status: 503 })));
    const res = await GET(req(), { params: { os: 'linux', arch: 'amd64' } });
    expect(res.status).toBe(503);
  });

  it('admin-api 에 닿지 못하면 502 로 구분해 준다', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new Error('ECONNREFUSED');
      }),
    );

    const res = await GET(req(), { params: { os: 'linux', arch: 'amd64' } });

    expect(res.status).toBe(502);
    expect(await res.text()).toContain('unreachable');
  });

  it('상류 파일명을 그대로 헤더에 되박지 않는다(헤더 분리·경로 방어)', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        upstream({
          headers: {
            // 따옴표·개행·경로 구분자가 섞인 악성 파일명.
            'content-disposition':
              'attachment; filename="../../etc/passwd"; x="y"',
          },
          body: streamOf('x'),
        }),
      ),
    );

    const res = await GET(req(), { params: { os: 'linux', arch: 'amd64' } });
    const cd = res.headers.get('content-disposition')!;

    expect(cd).not.toContain('/');
    expect(cd).not.toContain('..' + '/');
    expect(cd).not.toMatch(/[\r\n]/);
    // 정규화 후 남은 문자만 쓴다.
    expect(cd).toBe('attachment; filename="....etcpasswd"');
  });

  it('상류가 파일명을 안 주면 우리가 만든 이름으로 저장된다', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => upstream({ body: streamOf('x') })));

    const res = await GET(req(), { params: { os: 'windows', arch: 'amd64' } });
    expect(res.headers.get('content-disposition')).toBe(
      'attachment; filename="gateway-cli-windows-amd64.zip"',
    );
  });

  it('os/arch 를 인코딩해 상류 경로에 넣는다', async () => {
    // url 파라미터를 선언해 둔다 — 선언이 없으면 mock.calls 가 빈 튜플 타입이라
    // calls[0][0] 이 타입 에러가 된다(tsc 로 실제로 잡혔다).
    const spy = vi.fn(async (_url: string | URL) => upstream({ body: streamOf('x') }));
    vi.stubGlobal('fetch', spy);

    await GET(req(), { params: { os: '../secret', arch: 'amd64' } });

    const calledUrl = String(spy.mock.calls[0][0]);
    expect(calledUrl).not.toContain('../');
    expect(calledUrl).toContain('%2F'); // '/' 가 인코딩됐다
  });

  it('상류가 Content-Length 를 안 주면 그 헤더를 만들어내지 않는다', async () => {
    // 없는 길이를 지어내면 브라우저가 전송을 조기에 끊는다.
    vi.stubGlobal('fetch', vi.fn(async () => upstream({ body: streamOf('abc') })));

    const res = await GET(req(), { params: { os: 'linux', arch: 'amd64' } });
    expect(res.headers.get('content-length')).toBeNull();
  });
});
