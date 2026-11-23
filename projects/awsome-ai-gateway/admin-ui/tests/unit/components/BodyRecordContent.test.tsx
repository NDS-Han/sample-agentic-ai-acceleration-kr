// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 요청 상세 — Claude Code 요청처럼 user 메시지에 주입 블록이 수천 줄 들어 있어도
 * 맨 위 요약에서 실제 입력('hi')과 응답을 바로 읽을 수 있어야 한다.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { describe, expect, it } from 'vitest';

import messages from '../../../messages/en.json';
import { BodyRecordContent } from '@/components/monitoring/bodyLogRender';
import type { BodyLogResponse } from '@/lib/actions/body-logs';

const reminder = `<system-reminder>\n${'skill: something\n'.repeat(2000)}</system-reminder>`;

const data: BodyLogResponse = {
  found: true,
  request_id: 'r-1',
  source: 'firehose',
  key: 'k',
  duplicates: 0,
  oversized: false,
  model_alias: 'claude-sonnet',
  requested_at: '2026-11-06T00:07:00Z',
  client: 'other',
  cost_usd: 0.08,
  input_tokens: 2,
  output_tokens: 14,
  cache_read_tokens: 30000,
  cache_write_tokens: 12000,
  record: {
    request_id: 'r-1',
    provider: 'bedrock',
    client: 'other',
    model_alias: 'claude-sonnet',
    status: 'success',
    is_streaming: false,
    request_body: {
      messages: [
        { role: 'user', content: [{ type: 'text', text: reminder }, { type: 'text', text: 'hi' }] },
      ],
    },
    response_body: { content: [{ type: 'text', text: 'Hello! How can I help?' }] },
  },
};

function renderIt() {
  return render(
    <NextIntlClientProvider locale="en" messages={messages as unknown as AbstractIntlMessages}>
      <BodyRecordContent data={data} />
    </NextIntlClientProvider>,
  );
}

describe('BodyRecordContent', () => {
  it('맨 위 요약에 마지막 사용자 텍스트와 응답 텍스트를 보여준다', () => {
    renderIt();
    expect(screen.getByText('Last user text')).toBeTruthy();
    // 'hi' 는 요약과 대화 스레드 두 곳에 나온다 — 요약이 주입 블록 없이 'hi' 만이어야 한다.
    expect(screen.getAllByText('hi').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText('Hello! How can I help?').length).toBeGreaterThanOrEqual(1);
  });

  it('주입 블록은 태그명·길이만 보이게 접혀 있고, 펼치면 원문이 보인다', () => {
    const { container } = renderIt();
    expect(container.textContent).not.toContain('skill: something');
    const toggle = screen.getByText(/<system-reminder>/).closest('button')!;
    fireEvent.click(toggle);
    expect(container.textContent).toContain('skill: something');
  });

  it('캐시 읽기/쓰기 토큰과 앱 표시 라벨을 보여준다', () => {
    const { container } = renderIt();
    expect(container.textContent).toContain('Cache read');
    expect(container.textContent).toContain('30K');
    expect(container.textContent).toContain('Cache write');
    expect(container.textContent).toContain('12K');
    expect(screen.getByText('Other')).toBeTruthy();
  });
});
