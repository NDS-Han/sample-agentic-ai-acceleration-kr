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
    long_context_threshold_tokens: null,
    long_context_input_price_per_1k: null,
    long_context_output_price_per_1k: null,
    long_context_cache_creation_5m_price_per_1k: null,
    long_context_cache_creation_1h_price_per_1k: null,
    long_context_cache_read_price_per_1k: null,
    has_pricing: true,
    max_tokens: null,
    context_window: null,
    created_at: '2026-10-01T00:00:00Z',
    description: null,
    display_name: null,
    allowed_clients: null,
    account_role_arn: null,
    external_id: null,
    region: null,
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

  it('account_role_arn 이 있으면 마스킹된 계정 배지를 보이고 없으면 숨긴다', () => {
    render(
      <ModelsTable
        models={[
          makeModel({
            alias: 'routed',
            display_name: 'Routed',
            account_role_arn: 'arn:aws:iam::610156625835:role/gw-test',
            region: 'ap-south-1',
          }),
          makeModel({ alias: 'plain', display_name: 'Plain' }),
        ]}
      />
    );
    // 12자리 계정 ID 가 아니라 앞뒤 마스킹 형태로 렌더된다.
    expect(screen.getByText('→6101…5835')).toBeInTheDocument();
    expect(screen.queryByText('610156625835')).not.toBeInTheDocument();
  });
});

describe('ModelsTable — 모델 표기 (P2-8)', () => {
  it('공급자 배지: OPENMODEL → OpenModel, Mantle 계열은 Bedrock Mantle 접두로 통일', () => {
    render(
      <ModelsTable
        models={[
          makeModel({ alias: 'a', provider: 'OPENMODEL' }),
          makeModel({ alias: 'b', provider: 'BEDROCK_MANTLE' }),
          makeModel({ alias: 'c', provider: 'BEDROCK_MANTLE_OPENAI' }),
          makeModel({ alias: 'd', provider: 'BEDROCK_RUNTIME_OPENAI' }),
        ]}
      />,
    );
    // 접힌 행에만 배지가 있다(상세 행은 닫힘).
    expect(screen.getByText('OpenModel')).toBeInTheDocument();
    expect(screen.queryByText('OPENMODEL')).not.toBeInTheDocument();
    expect(screen.getByText('Bedrock Mantle')).toBeInTheDocument();
    expect(screen.getByText('Bedrock Mantle · OpenAI')).toBeInTheDocument();
    expect(screen.queryByText('Mantle · OpenAI')).not.toBeInTheDocument();
    expect(screen.getByText('Bedrock · OpenAI')).toBeInTheDocument();
  });

  it('장문 구간 배지는 hover/focus 툴팁으로 의미를 설명한다', () => {
    render(<ModelsTable models={[makeModel({ long_context_threshold_tokens: 272000 })]} />);
    const trigger = screen.getByRole('button', { name: />272K: longContextBadgeTitle/ });
    expect(trigger).toHaveTextContent('>272K');
    expect(screen.getByRole('tooltip', { hidden: true })).toHaveTextContent('longContextBadgeTitle');
  });

  it('alias === model_id 행에 경고 배지 — 같은 upstream ID 의 다른 alias 를 안내한다', () => {
    render(
      <ModelsTable
        models={[
          makeModel({ alias: 'claude-opus-4-8', model_id: 'global.anthropic.claude-opus-4-8' }),
          makeModel({
            alias: 'global.anthropic.claude-opus-4-8',
            model_id: 'global.anthropic.claude-opus-4-8',
          }),
        ]}
      />,
    );
    const badges = screen.getAllByText('upstreamAliasBadge');
    expect(badges).toHaveLength(1);
    expect(screen.getByRole('tooltip', { hidden: true })).toHaveTextContent(
      'upstreamAliasHint upstreamAliasSame',
    );
  });

  it('alias !== model_id 이면 경고 배지가 없다', () => {
    render(<ModelsTable models={[makeModel({})]} />);
    expect(screen.queryByText('upstreamAliasBadge')).not.toBeInTheDocument();
  });
});
