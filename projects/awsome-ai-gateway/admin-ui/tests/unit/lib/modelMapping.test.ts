// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { describe, it, expect } from 'vitest';
import { mapToModelListItem, type APIModelItem } from '@/lib/utils/modelMapping';

// models/page.tsx 와 budgets/page.tsx 에 복붙돼 있던 매핑의 단일 출처.
// 특히 가격 필드(null → 0)와 status → is_active 변환은 예산/다운그레이드
// 셀렉트의 정확성에 직결된다.
describe('mapToModelListItem', () => {
  const base: APIModelItem = {
    alias: 'claude-sonnet-5',
    provider: 'bedrock',
    provider_model_id: 'anthropic.claude-sonnet-5',
    endpoint_url: null,
    status: 'ACTIVE',
    description: 'desc',
    display_name: 'Sonnet 5',
    current_pricing: {
      input_price_per_1k_tokens: '0.003',
      output_price_per_1k_tokens: '0.015',
      cache_read_price_per_1k_tokens: '0.0003',
    },
    context_window: 200000,
    max_output_tokens: 64000,
  };

  it('가격 문자열을 숫자로 변환하고 ACTIVE 를 is_active 로 매핑한다', () => {
    const m = mapToModelListItem(base);
    expect(m.is_active).toBe(true);
    expect(m.input_price_per_1k).toBeCloseTo(0.003);
    expect(m.output_price_per_1k).toBeCloseTo(0.015);
    expect(m.cache_read_price_per_1k).toBeCloseTo(0.0003);
    expect(m.cache_creation_5m_price_per_1k).toBe(0); // 누락 필드 → 0
    expect(m.max_tokens).toBe(64000);
    expect(m.context_window).toBe(200000);
  });

  it('current_pricing null → 모든 가격 0, status ≠ ACTIVE → is_active false', () => {
    const m = mapToModelListItem({ ...base, current_pricing: null, status: 'DEPRECATED' });
    expect(m.is_active).toBe(false);
    expect(m.input_price_per_1k).toBe(0);
    expect(m.output_price_per_1k).toBe(0);
  });

  it('null 필드는 기본값으로 떨어진다', () => {
    const m = mapToModelListItem({
      ...base,
      max_output_tokens: null,
      context_window: null,
      endpoint_url: 'https://x',
    });
    expect(m.max_tokens).toBe(0);
    expect(m.context_window).toBe(0);
    expect(m.endpoint_url).toBe('https://x');
  });
});
