// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 요청 에러율 계산 — 대시보드/분석/내 사용량 KPI 공통.
 *
 * status 는 SUCCESS|ERROR|TIMEOUT 세 가지뿐이라 전체 = 성공 + 에러.
 * 정책 차단(429·예산 HARD_BLOCK·인증 실패)은 usage_logs 에 행이 없어
 * 에러율을 부풀리지 않는다 — ERROR/TIMEOUT 은 상류·게이트웨이 장애만.
 *
 * 경보는 "1건의 에러로 빨간 카드"가 되지 않도록 볼륨+비율 동시 조건이다.
 */
export interface ErrorRateInfo {
  /** 성공 + 에러 (전체 요청) */
  total: number;
  /** ERROR + TIMEOUT */
  errors: number;
  /** 에러율 % (0-100, 소수 1자리 반올림 전 원값) */
  rate: number;
  /** 전체 ≥50건 && 에러율 ≥2% — 이 조합만 시각적 경고를 켠다 */
  warn: boolean;
}

export function errorRateInfo(success: number, errors: number): ErrorRateInfo {
  const total = success + errors;
  const rate = total > 0 ? (errors / total) * 100 : 0;
  return { total, errors, rate, warn: total >= 50 && rate >= 2 };
}
