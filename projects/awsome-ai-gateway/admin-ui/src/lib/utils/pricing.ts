// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 가격 표시 단위 — DB/백엔드는 USD per-1K 토큰으로 저장하지만, 화면은
 * LiteLLM 카탈로그와 같은 per-1M 토큰 표기로 통일한다 (×1000).
 *
 * 입력 방향(다이얼로그)은 반대: 사용자가 /1M 값을 넣으면 /1K 로 환산해 저장한다.
 */

export function fmtPricePerM(per1k: number | string | null | undefined): string {
  if (per1k == null || per1k === '') return '—';
  const per1m = Number(per1k) * 1000;
  if (!Number.isFinite(per1m)) return '—';
  const str =
    per1m >= 1000 ? per1m.toFixed(0)
    : per1m >= 1 ? per1m.toFixed(2)
    : _trimDecimals(per1m, 5, 3);
  return `$${str}/M`;
}

/**
 * 소수 최대 `max`자리까지 보이되 끝의 0 은 지우고 최소 `min`자리는 둔다.
 * NUMERIC(12,8) per-1K 는 per-1M 환산 시 소수 5자리까지 의미가 있다 —
 * Haiku 5.5 캐시 쓰기 $0.1375/M 처럼 toFixed(3) 으로 잘리는 값이 있다(US-19).
 */
function _trimDecimals(v: number, max: number, min: number): string {
  const [whole, frac = ''] = v.toFixed(max).split('.');
  let f = frac.replace(/0+$/, '');
  if (f.length < min) f = f.padEnd(min, '0');
  return `${whole}.${f}`;
}

// ⚠️ per-1K↔per-1M 입력 변환은 CreateModelDialog 의 perMtoK/perKtoM 만 쓴다 —
// 여기에 또 두면 float64 잔여/과학 표기(1e-7) 분기가 갈린다. (US-19 리뷰에서 제거)
