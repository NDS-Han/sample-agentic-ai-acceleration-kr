// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /admin/models 응답 → ModelListItem 매핑의 단일 출처.
 * 예전에는 models/page.tsx 와 budgets/page.tsx 에 동일 코드가 복붙돼 있었다.
 */

import type { ModelListItem } from '@/types/entities';

export interface APIModelItem {
  alias: string;
  provider: string;
  provider_model_id: string;
  endpoint_url: string | null;
  status: string;
  description: string | null;
  display_name: string | null;
  current_pricing: {
    input_price_per_1k_tokens: string;
    output_price_per_1k_tokens: string;
    cache_creation_5m_price_per_1k_tokens?: string;
    cache_creation_1h_price_per_1k_tokens?: string;
    cache_read_price_per_1k_tokens?: string;
  } | null;
  context_window: number | null;
  max_output_tokens: number | null;
}

export function mapToModelListItem(item: APIModelItem): ModelListItem {
  const p = item.current_pricing;
  return {
    alias: item.alias,
    provider: item.provider,
    model_id: item.provider_model_id,
    endpoint_url: item.endpoint_url ?? null,
    is_active: item.status === 'ACTIVE',
    input_price_per_1k: p ? parseFloat(p.input_price_per_1k_tokens) : 0,
    output_price_per_1k: p ? parseFloat(p.output_price_per_1k_tokens) : 0,
    cache_creation_5m_price_per_1k: p?.cache_creation_5m_price_per_1k_tokens
      ? parseFloat(p.cache_creation_5m_price_per_1k_tokens)
      : 0,
    cache_creation_1h_price_per_1k: p?.cache_creation_1h_price_per_1k_tokens
      ? parseFloat(p.cache_creation_1h_price_per_1k_tokens)
      : 0,
    cache_read_price_per_1k: p?.cache_read_price_per_1k_tokens
      ? parseFloat(p.cache_read_price_per_1k_tokens)
      : 0,
    max_tokens: item.max_output_tokens ?? 0,
    context_window: item.context_window ?? 0,
    description: item.description,
    display_name: item.display_name,
  };
}
