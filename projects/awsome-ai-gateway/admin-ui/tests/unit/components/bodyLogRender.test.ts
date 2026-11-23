// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { describe, it, expect } from 'vitest';

import {
  splitJsonFrames,
  extractSseParts,
  decodeUnicodeEscapes,
  splitInjectedBlocks,
  stripInjectedBlocks,
  lastUserText,
  conversationTurns,
  cacheTokenParts,
  fmtTokenFlow,
  clientDisplay,
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
    // 진짜 역슬래시+u 문자열(\\u)은 디코드하지 않는다 — \\u0041 은 유효한
    // 이스케이프처럼 보이지만 선행 역슬래시가 리터럴이므로 보존돼야 한다.
    expect(decodeUnicodeEscapes('\\\\u0041')).toBe('\\\\u0041');
  });
});

describe('주입 블록 접기 — Claude Code user 메시지', () => {
  const reminder = '<system-reminder>\n' + 'skill list\n'.repeat(500) + '</system-reminder>';
  const text = `${reminder}\n<local-command-stdout>ok</local-command-stdout>\nhi`;

  it('줄 맨 앞에서 시작해 같은 이름으로 닫히는 태그 블록을 분리한다', () => {
    const segs = splitInjectedBlocks(text);
    expect(segs.filter((s) => s.kind === 'block').map((s) => (s.kind === 'block' ? s.tag : ''))).toEqual([
      'system-reminder',
      'local-command-stdout',
    ]);
    // 원문 보존 — 세그먼트를 이으면 원래 텍스트다.
    expect(segs.map((s) => s.text).join('')).toBe(text);
  });

  it('문장 중간의 태그나 닫히지 않은 태그는 건드리지 않는다', () => {
    const plain = 'use <div> here\n<open-only>';
    expect(splitInjectedBlocks(plain)).toEqual([{ kind: 'text', text: plain }]);
  });

  it('stripInjectedBlocks 는 실제 입력만 남긴다', () => {
    expect(stripInjectedBlocks(text)).toBe('hi');
  });

  it('lastUserText — 툴 결과만 있는 턴은 건너뛰고 마지막 실제 입력을 찾는다', () => {
    const turns = conversationTurns({
      messages: [
        { role: 'user', content: [{ type: 'text', text: reminder }, { type: 'text', text: 'hi' }] },
        { role: 'assistant', content: [{ type: 'tool_use', name: 'Read', input: {} }] },
        { role: 'user', content: [{ type: 'tool_result', content: 'file body' }] },
      ],
    });
    expect(lastUserText(turns)).toBe('hi');
  });
});

describe('토큰·캐시·앱 표기', () => {
  it('입력→출력', () => {
    expect(fmtTokenFlow(2, 14)).toBe('2→14');
    expect(fmtTokenFlow(null, null)).toBeNull();
  });

  it('캐시 토큰이 있을 때만 읽기/쓰기를 낸다', () => {
    expect(cacheTokenParts(30000, 12000)).toEqual({ read: '30K', write: '12K' });
    expect(cacheTokenParts(0, 0)).toBeNull();
    expect(cacheTokenParts(null, undefined)).toBeNull();
  });

  it("앱은 다른 화면과 같은 표시 라벨 — 'other' → 'Other'", () => {
    expect(clientDisplay('other')).toBe('Other');
    expect(clientDisplay('claude-code')).toBe('Claude Code');
    expect(clientDisplay(null)).toBeNull();
  });
});
