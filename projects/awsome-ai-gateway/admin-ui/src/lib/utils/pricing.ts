// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 가격 표시 단위 — DB/백엔드는 USD per-1K 토큰으로 저장하지만, 화면은
 * LiteLLM 카탈로그와 같은 per-1M 토큰 표기로 통일한다 (×1000).
 *
 * 입력 방향(다이얼로그)은 반대: 사용자가 /1M 값을 넣으면 /1K 로 환산해 저장한다.
 */

import { fmtUsdRate } from './format';

export function fmtPricePerM(per1k: number | string | null | undefined): string {
  if (per1k == null || per1k === '') return '—';
  const per1m = Number(per1k) * 1000;
  if (!Number.isFinite(per1m)) return '—';
  // 앱 공용 USD 규칙(fmtUsd)과 같은 기준 — ≥$0.01 은 최소 2자리, <$0.01 은
  // 유효숫자 2개. 예전엔 ≥$1 은 2자리, 미만은 3자리 패딩이라 $2.00/M 옆에
  // $0.100/M 이 섞였다. NUMERIC(12,8) per-1K 는 per-1M 환산 시 소수 5자리까지
  // 의미가 있어 끝 0 만 지우고 5자리까지 보존한다($0.1375/M, US-19).
  return `${fmtUsdRate(per1m, 5)}/M`;
}

// ⚠️ per-1K↔per-1M 입력 변환은 CreateModelDialog 의 perMtoK/perKtoM 만 쓴다 —
// 여기에 또 두면 float64 잔여/과학 표기(1e-7) 분기가 갈린다. (US-19 리뷰에서 제거)
