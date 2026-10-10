// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 1K 토큰당 단가 표시. DB 단가 열은 소수 8자리(NUMERIC(12,8))다 — Haiku 5.5 의 US 캐시 쓰기
 * 단가 0.0001375 처럼 6자리를 넘는 값이 있다. toFixed(4/5/6) 은 그런 값을 잘라 보였다
 * (0.00011 → "0.0001"). 8자리까지 보이고 끝의 0 은 줄이되, 익숙한 모양을 위해 최소 4자리는 둔다.
 */
export function formatRate(n: number): string {
  const s = n.toFixed(8);
  const [whole, frac = ''] = s.split('.');
  let f = frac.replace(/0+$/, '');
  if (f.length < 4) f = f.padEnd(4, '0');
  return `${whole}.${f}`;
}
