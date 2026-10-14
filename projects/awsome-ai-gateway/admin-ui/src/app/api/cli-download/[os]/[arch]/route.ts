// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * CLI 패키지 다운로드 프록시.
 *
 * admin-api 는 브라우저에 노출되지 않으므로(내부 DNS/CORS) /cli 화면의 다운로드
 * 버튼은 반드시 같은 오리진의 이 라우트를 가리켜야 한다. app/cli/page.tsx 가
 * admin-api 의 download_url(`/cli/download/...`)을 `/api/cli-download/...` 로
 * 바꿔 넘기는 이유다.
 *
 * 정정: 예전 경로가 Next 404 였던 것은 **아니다**. app/cli/download/[os]/[arch]/route.ts 가
 * 같은 경로를 받고 있었고, 버튼이 404 였던 진짜 원인은 admin-api 의 CLI_DIST_DIR 이 비어
 * 있어 상류가 404 였던 것이다. 그 중복 프록시는 버퍼링(res.blob())에 502 구분도 없어서
 * 제거했고, 중계 라우트가 다시 둘로 갈라지지 않도록 tests/unit/navigation.test.ts 가
 * 개수를 못 박는다.
 */

import { NextRequest } from 'next/server';
import { upstreamErrorStatus } from '@/lib/utils/upstream';

// 파일 스트림 라우트 — 캐시/정적최적화 금지, node 런타임에서 body 를 그대로 흘린다.
export const dynamic = 'force-dynamic';
export const runtime = 'nodejs';
export const fetchCache = 'force-no-store';

const ADMIN_API_URL = process.env.ADMIN_API_URL || 'http://admin-api:8080';

// R17 nit — os/arch 는 URL 세그먼트다. encodeURIComponent('..') 는 '..' 그대로라
// `/cli/download/../..` 가 URL 정규화로 `/` 까지 탈출한다 — admin-api 의 화이트
// 리스트가 실제 피해를 막지만 경계 의도상 세그먼트 문자셋으로 먼저 거른다.
const SAFE_SEGMENT = /^[a-z0-9_-]+$/i;

export async function GET(
  _request: NextRequest,
  props: { params: Promise<{ os: string; arch: string }> }
) {
  const params = await props.params;
  if (!SAFE_SEGMENT.test(params.os) || !SAFE_SEGMENT.test(params.arch)) {
    return new Response('Invalid download path', { status: 400 });
  }
  const os = encodeURIComponent(params.os);
  const arch = encodeURIComponent(params.arch);

  let res: Response;
  try {
    res = await fetch(`${ADMIN_API_URL}/cli/download/${os}/${arch}`, {
      cache: 'no-store',
    });
  } catch (e) {
    // admin-api 에 닿지 못한 경우 — 502/504 로 구분해 준다(404 로 뭉개면 원인 추적 불가).
    return new Response('Admin API unreachable', { status: upstreamErrorStatus(e) });
  }

  if (!res.ok) {
    // 상태코드를 그대로 전달한다 — 404(패키지 미빌드)와 5xx 를 구분할 수 있게.
    return new Response(`Download not available (upstream ${res.status})`, {
      status: res.status,
    });
  }

  const fallbackName = `gateway-cli-${params.os}-${params.arch}${
    params.os.toLowerCase() === 'windows' ? '.zip' : '.tar.gz'
  }`;
  // 상류 헤더에서 온 값이라 그대로 다시 헤더에 넣지 않는다. 따옴표·개행·경로 구분자가 섞이면
  // Content-Disposition 이 깨지거나(헤더 분리) 이상한 파일명으로 저장된다. 화이트리스트로
  // 정규화하고, 남는 게 없으면 우리가 만든 이름을 쓴다.
  const upstreamName = res.headers
    .get('content-disposition')
    ?.match(/filename="?([^";]+)"?/)?.[1]
    ?.replace(/[^A-Za-z0-9._-]/g, '');
  const filename = upstreamName && upstreamName.length > 0 ? upstreamName : fallbackName;

  const headers = new Headers({
    // 업스트림 타입을 신뢰한다 — windows 는 zip, 나머지는 gzip 이라 하드코딩하면 어긋난다.
    'Content-Type': res.headers.get('content-type') ?? 'application/octet-stream',
    'Content-Disposition': `attachment; filename="${filename}"`,
    'Cache-Control': 'no-store',
  });
  const length = res.headers.get('content-length');
  if (length) headers.set('Content-Length', length); // 브라우저 진행률 표시

  // ⚠️ body 를 스트림으로 넘긴다. 예전엔 res.blob() 으로 패키지 전체를 메모리에 올린 뒤
  //    다시 내려보냈다(패키지가 커질수록 admin-ui 파드 메모리를 그대로 먹는다).
  return new Response(res.body, { status: 200, headers });
}
