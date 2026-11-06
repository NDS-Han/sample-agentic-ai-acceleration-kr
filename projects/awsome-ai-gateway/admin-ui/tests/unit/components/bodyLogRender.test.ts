// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { describe, it, expect } from 'vitest';

import {
  splitJsonFrames,
  extractSseParts,
  decodeUnicodeEscapes,
} from '@/components/monitoring/bodyLogRender';

describe('splitJsonFrames', () => {
  it('개행 없이 이어 붙은 JSON 프레임을 분리한다 (/model/* passthrough)', () => {
    const sse = '{"a":1}{"b":{"c":"}"}}{"d":"x\\"}y"}';
    expect(splitJsonFrames(sse)).toEqual([
      '{"a":1}',
      '{"b":{"c":"}"}}',
      '{"d":"x\\"}y"}',
    ]);
  });

  it('data: 접두와 공백·잡음을 무시한다', () => {
    const sse = 'data: {"type":"a"}\n\ndata: {"type":"b"}\ndata: [DONE]\n';
    expect(splitJsonFrames(sse)).toEqual(['{"type":"a"}', '{"type":"b"}']);
  });
});

describe('extractSseParts', () => {
  it('Anthropic 스트림에서 텍스트와 tool_use(input_json_delta)를 재조립한다', () => {
    const frames = [
      { type: 'content_block_start', index: 0, content_block: { type: 'text', text: '' } },
      { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: '안녕' } },
      { type: 'content_block_delta', index: 0, delta: { type: 'text_delta', text: '하세요' } },
      { type: 'content_block_stop', index: 0 },
      {
        type: 'content_block_start',
        index: 1,
        content_block: { type: 'tool_use', id: 't1', name: 'web_search', input: {} },
      },
      {
        type: 'content_block_delta',
        index: 1,
        delta: { type: 'input_json_delta', partial_json: '{"query":"SK' },
      },
      {
        type: 'content_block_delta',
        index: 1,
        delta: { type: 'input_json_delta', partial_json: '하이닉스"}' },
      },
      { type: 'content_block_stop', index: 1 },
    ];
    const sse = frames.map((f) => `data: ${JSON.stringify(f)}`).join('\n');
    const parts = extractSseParts(sse);
    expect(parts.text).toBe('안녕하세요');
    expect(parts.toolCalls).toEqual([
      { name: 'web_search', input: { query: 'SK하이닉스' } },
    ]);
  });

  it('Bedrock Converse 래퍼를 조립한다 — contentBlockIndex 는 래퍼 안', () => {
    // /model/* 는 이벤트를 개행 없이 이어 붙인다.
    const sse =
      '{"contentBlockStart":{"contentBlockIndex":0,"start":{"toolUse":{"name":"ls"}}}}' +
      '{"contentBlockDelta":{"contentBlockIndex":0,"delta":{"toolUse":{"input":"{\\"path\\":"}}}}' +
      '{"contentBlockDelta":{"contentBlockIndex":0,"delta":{"toolUse":{"input":"\\"/tmp\\"}"}}}}' +
      '{"contentBlockStop":{"contentBlockIndex":0}}' +
      '{"contentBlockStart":{"contentBlockIndex":1,"start":{}}}' +
      '{"contentBlockDelta":{"contentBlockIndex":1,"delta":{"text":"done"}}}' +
      '{"contentBlockStop":{"contentBlockIndex":1}}';
    const parts = extractSseParts(sse);
    expect(parts.text).toBe('done');
    expect(parts.toolCalls).toEqual([{ name: 'ls', input: { path: '/tmp' } }]);
  });
});

describe('decodeUnicodeEscapes', () => {
  it('와이어 \\uXXXX 시퀀스를 디코드하고 리터럴 백슬래시-u 는 보존한다', () => {
    expect(decodeUnicodeEscapes('SK\\ud558\\uc774\\ub2c9\\uc2a4')).toBe('SK하이닉스');
    // 진짜 역슬래시+u 문자열(\\u)은 디코드하지 않는다
    expect(decodeUnicodeEscapes('C:\\\\users\\\\x')).toBe('C:\\\\users\\\\x');
  });
});
