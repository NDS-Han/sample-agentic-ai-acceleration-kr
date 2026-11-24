// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * AgentCore 웹 검색 단가 입력 검증 — 0~1000 USD / 1,000 queries, 소수 4자리까지.
 * 백엔드 WebSearchPricingUpdateRequest 와 같은 범위(서버 액션·카드가 공유).
 */
const PRICE_PATTERN = /^\d{1,4}(\.\d{1,4})?$/;

export function isValidWebSearchPrice(raw: string): boolean {
  const v = raw.trim();
  return PRICE_PATTERN.test(v) && Number(v) <= 1000;
}
