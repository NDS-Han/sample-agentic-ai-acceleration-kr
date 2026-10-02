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
import { render, screen } from '@testing-library/react';
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
    max_tokens: 0,
    context_window: 0,
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
