// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 모델 테이블 — 모델×앱 제한(allowed_clients) 읽기 전용 배지.
 *
 * 배경: model_aliases.allowed_clients( null=무제한 / []=전면 차단 / 목록=그 앱만 )은
 * admin-api·effective-policy·게이트웨이 인가가 전부 아는 축인데 /models 에는 표시가
 * 없어, codex 요청이 model_app 축에서 거부돼도 운영자가 이 화면에서 원인을 못 본다.
 * 배지는 3-상태를 구분해야 한다 — [] 를 "무제한"처럼 보이게 하면 전면 차단이 숨겨진다.
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

describe('ModelsTable — allowed_clients 배지', () => {
  it('null(무제한)이면 배지를 렌더하지 않는다', () => {
    render(<ModelsTable models={[makeModel({ allowed_clients: null })]} />);
    expect(screen.queryByText('appScopeRestricted')).toBeNull();
    expect(screen.queryByText('appScopeBlocked')).toBeNull();
  });

  it('목록이 있으면 제한 배지를 /apps 링크로 렌더한다', () => {
    render(
      <ModelsTable
        models={[makeModel({ allowed_clients: ['codex', 'claude-code'] })]}
      />
    );
    const link = screen.getByText('appScopeRestricted').closest('a');
    expect(link).not.toBeNull();
    expect(link?.getAttribute('href')).toBe('/apps');
    expect(link?.getAttribute('title')).toBe('codex, claude-code');
  });

  it('빈 목록 [] 은 전면 차단 배지로 구분한다', () => {
    render(<ModelsTable models={[makeModel({ allowed_clients: [] })]} />);
    expect(screen.getByText('appScopeBlocked')).not.toBeNull();
    expect(screen.queryByText('appScopeRestricted')).toBeNull();
  });
});

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

  it('펼친 행에는 provider_model_id 와 앱 범위 상세가 보인다', () => {
    render(
      <ModelsTable
        models={[makeModel({ allowed_clients: ['codex', 'cowork'] })]}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'expand' }));
    expect(screen.getByText('anthropic.claude-x')).not.toBeNull();
    expect(screen.getByText('codex')).not.toBeNull();
    expect(screen.getByText('cowork')).not.toBeNull();
  });
});
