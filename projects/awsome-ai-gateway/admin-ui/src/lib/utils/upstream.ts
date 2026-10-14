// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * BFF → admin-api upstream fetch 공통 규칙 (R17-S6).
 *
 * 기본 fetch 에는 타임아웃이 없다 — admin-api 가 hang 하면 RSC 렌더와 프록시
 * 요청이 소켓/메모리를 무기한 붙잡아 장애가 연쇄된다. 모든 프록시/RSC 호출은
 * 이 헬퍼로 타임아웃을 건다(기본 15s — 대시보드 최악 쿼리도 초 단위).
 *
 * ⚠️ 타임아웃은 **응답 헤더 도착까지**만 적용한다. `AbortSignal.timeout` 은
 *    헤더 도착 후에도 발동해 스트림 본문(analytics-export CSV, cli-download
 *    바이너리)을 중간에 끊는다 — 헤더가 오면 타이머를 해제하고 본문은 끝까지
 *    흘린다. 호출자가 준 signal 도 함께 존중한다(AbortSignal.any).
 */
export const UPSTREAM_TIMEOUT_MS = 15_000;

export async function fetchUpstream(
  url: string | URL,
  init?: RequestInit,
  timeoutMs: number = UPSTREAM_TIMEOUT_MS,
): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  const signal = init?.signal
    ? AbortSignal.any([init.signal, controller.signal])
    : controller.signal;
  try {
    return await fetch(url, { ...init, signal });
  } finally {
    clearTimeout(timer);
  }
}

/**
 * upstream fetch 실패의 HTTP 상태 매핑 — 타임아웃은 504, 연결 거부/DNS 등
 * 나머지 전송 실패는 502. catch 에서 status 만 뽑을 때 쓴다.
 */
export function upstreamErrorStatus(err: unknown): 502 | 504 {
  const name = (err as { name?: string })?.name;
  return name === 'TimeoutError' || name === 'AbortError' ? 504 : 502;
}
