// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * middleware CSP — CHAT_IMAGE_HOSTS 필터 회귀 (kiro R17-5 S1 / R17-6 N2 / R17-7 N-2).
 *
 * `SECURITY_HEADERS` 는 모듈 초기화 시점에 env 를 읽으므로 케이스마다
 * vi.resetModules() + 동적 import 로 격리한다.
 */

import { describe, it, expect, vi, afterEach } from 'vitest';
import { NextRequest } from 'next/server';

async function cspFor(hosts: string | undefined): Promise<string> {
  vi.resetModules();
  if (hosts === undefined) {
    delete process.env.CHAT_IMAGE_HOSTS;
  } else {
    process.env.CHAT_IMAGE_HOSTS = hosts;
  }
  const { middleware } = await import('@/middleware');
  // 쿠키 없는 요청은 로그인 리다이렉트 — 리다이렉트 응답에도 보안 헤더가 붙는다.
  const res = await middleware(new NextRequest('http://admin.test/'));
  const csp = res.headers.get('content-security-policy');
  expect(csp).not.toBeNull();
  return csp!;
}

const imgSrc = (csp: string) =>
  csp.split(';').find((d) => d.trim().startsWith('img-src')) ?? '';

afterEach(() => {
  delete process.env.CHAT_IMAGE_HOSTS;
});

describe('middleware CSP — CHAT_IMAGE_HOSTS', () => {
  it('미설정이면 외부 이미지 전면 차단(fail-closed)', async () => {
    const img = imgSrc(await cspFor(undefined));
    expect(img).not.toContain('amazonaws.com');
    expect(img).not.toContain('https://');
  });

  it('virtual-hosted 버킷 호스트는 허용된다', async () => {
    const img = imgSrc(await cspFor('mybucket.s3.ap-south-1.amazonaws.com'));
    expect(img).toContain('https://mybucket.s3.ap-south-1.amazonaws.com');
  });

  it('path-style 리전 엔드포인트는 거부된다 — 모든 버킷 경로가 열리는 회귀', async () => {
    for (const bad of [
      's3.ap-south-1.amazonaws.com',
      's3-ap-south-1.amazonaws.com',
      's3.amazonaws.com',
      'S3.ap-south-1.amazonaws.com',
      'vpce-0123.s3.ap-south-1.vpce.amazonaws.com',
    ]) {
      const img = imgSrc(await cspFor(bad));
      expect(img).not.toContain(bad.toLowerCase());
      expect(img.toLowerCase()).not.toContain(bad.toLowerCase());
    }
  });

  it('디렉티브 인젝션/와일드카드/scheme 포함 값은 폐기된다', async () => {
    const img = imgSrc(
      await cspFor(
        "ok.example.com; script-src 'unsafe-eval' ,*.amazonaws.com,https://evil.example.com,good.example.com"
      )
    );
    expect(img).toContain('good.example.com');
    expect(img).not.toContain('unsafe-eval');
    expect(img).not.toContain('evil.example.com');
    expect(img).not.toContain('amazonaws.com');
  });
});
