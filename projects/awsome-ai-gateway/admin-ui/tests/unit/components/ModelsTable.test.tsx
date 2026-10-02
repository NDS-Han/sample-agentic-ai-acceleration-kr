// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 테이블 — LiteLLM 스타일 확장 행 + null 안전 렌더.
 *
 * 모델×앱 제한(allowed_clients) 표시는 /apps 소유로 /models 에서는 제거됐다.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ModelsTable } from '@/components/models/ModelsTable';
import type { ModelListItem } from '@/types/entities';

vi.mock('next-intl', () => ({
  useTranslations: () => (key: string) => key,
  useLocale: () => 'en',
}));

vi.mock('@/lib/actions/models', () => ({
  activateModelAction: vi.fn(),
}));

const toast = vi.fn();
vi.mock('@/components/common/ToastProvider', () => ({
  useToast: () => ({ toast }),
}));

function makeModel(overrides: Partial<ModelListItem>): ModelListItem {
  return {
    alias: 'm1',
    provider: 'BEDROCK',
    model_id: 'anthropic.claude-x',
    endpoint_url: null,
    is_active: true,
    input_price_per_1k: 0.003,
    output_price_per_1k: 0.015,
    cache_creation_5m_price_per_1k: 0,
    cache_creation_1h_price_per_1k: 0,
    cache_read_price_per_1k: 0,
    has_pricing: true,
    max_tokens: null,
    context_window: null,
    created_at: '2026-10-01T00:00:00Z',
    description: null,
    display_name: null,
    allowed_clients: null,
    ...overrides,
  };
}

describe('ModelsTable — LiteLLM 스타일 확장 행', () => {
  it('확장 버튼은 aria-expanded 를 가지고 클릭 시 상세 행을 연다', () => {
    render(<ModelsTable models={[makeModel({ display_name: 'Claude Sonnet' })]} />);
    const btn = screen.getByRole('button', { name: 'expand' });
    expect(btn.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(btn);
    expect(btn.getAttribute('aria-expanded')).toBe('true');
    expect(btn.getAttribute('aria-controls')).toBe('model-detail-m1');
    expect(document.getElementById('model-detail-m1')).not.toBeNull();
  });

  it('다시 클릭하면 상세 행이 닫힌다', () => {
    render(<ModelsTable models={[makeModel({})]} />);
    const btn = screen.getByRole('button', { name: 'expand' });
    fireEvent.click(btn);
    fireEvent.click(screen.getByRole('button', { name: 'collapse' }));
    expect(document.getElementById('model-detail-m1')).toBeNull();
  });

  it('가격 행이 없는 모델은 $0.00 이 아니라 — 로 표시한다', () => {
    render(<ModelsTable models={[makeModel({ has_pricing: false })]} />);
    expect(screen.queryByText(/\$0\.0/)).toBeNull();
  });

  it('스펙 미등록 모델은 0K 가 아니라 — 로 표시한다', () => {
    render(<ModelsTable models={[makeModel({ context_window: null })]} />);
    expect(screen.queryByText('0K')).toBeNull();
  });

  it('컨텍스트는 K 접미사로 포맷된다', () => {
    render(<ModelsTable models={[makeModel({ context_window: 200000 })]} />);
    expect(screen.getByText('200K')).not.toBeNull();
  });

  it('model_id 는 접힌 행과 펼친 패널 둘 다에 보인다', () => {
    render(<ModelsTable models={[makeModel({})]} />);
    fireEvent.click(screen.getByRole('button', { name: 'expand' }));
    expect(screen.getAllByText('anthropic.claude-x').length).toBe(2);
  });

  it('alias === model_id 이면 접힌 행에서 중복 표시하지 않는다', () => {
    render(<ModelsTable models={[makeModel({ alias: 'same-id', model_id: 'same-id', display_name: 'Nice Name' })]} />);
    expect(screen.getAllByText('same-id').length).toBe(1);
  });
});
