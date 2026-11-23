// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Body Logs 다듬기 — 요약 태그 잔재, 들여쓰기·인라인 command-* 접기, 긴 블록 접기,
 * 세션 trace 의 상태/캐시 R/W 셀.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { NextIntlClientProvider, type AbstractIntlMessages } from 'next-intl';
import { describe, expect, it } from 'vitest';

import messages from '../../../messages/en.json';
import {
  CacheCell,
  CallStatusBadge,
  LONG_TEXT_MAX_CHARS,
  LONG_TEXT_MAX_LINES,
  LongText,
  conversationTurns,
  isLongText,
  lastUserText,
  longTextPreview,
  splitInjectedBlocks,
  stripInjectedBlocks,
} from '@/components/monitoring/bodyLogRender';

function wrap(node: React.ReactNode) {
  return (
    <NextIntlClientProvider locale="en" messages={messages as unknown as AbstractIntlMessages}>
      {node}
    </NextIntlClientProvider>
  );
}

// Claude Code 가 /model 실행 시 실제로 보내는 형태(트랜스크립트 원문) — 두 번째
// 태그부터 들여쓰기로 붙는다.
const SLASH_MODEL =
  '<command-name>/model</command-name>\n' +
  '            <command-message>model</command-message>\n' +
  '            <command-args></command-args>';
const STDOUT = '<local-command-stdout>Set model to Opus</local-command-stdout>';

describe('요약 — 마지막 사용자 텍스트', () => {
  it("Claude Code /model 요청 — 블록 ['hi', /model, stdout, 'hi'] 에서 'hi' 만", () => {
    const turns = conversationTurns({
      messages: [
        {
          role: 'user',
          content: [
            { type: 'text', text: 'hi' },
            { type: 'text', text: SLASH_MODEL },
            { type: 'text', text: STDOUT },
            { type: 'text', text: 'hi' },
          ],
        },
      ],
    });
    expect(lastUserText(turns)).toBe('hi');
  });

  it('한 문자열에 섞여 있어도 들여쓰기·인라인 태그를 모두 지운다', () => {
    expect(stripInjectedBlocks(`${SLASH_MODEL}\n${STDOUT}\nhi`)).toBe('hi');
    expect(stripInjectedBlocks(`hi <system-reminder>x</system-reminder> there`)).toBe(
      'hi  there',
    );
    // 자기 완결 태그, 중첩, 공백만 남은 줄 정리
    expect(stripInjectedBlocks('<foo/>\n  <bar a="1" />\n\n\n\nhi')).toBe('hi');
    expect(stripInjectedBlocks('<a><b>x</b></a>\n  \nhi')).toBe('hi');
  });

  it('태그뿐인 마지막 블록은 건너뛰고 앞 블록의 실제 텍스트', () => {
    const turns = conversationTurns({
      messages: [{ role: 'user', content: [{ type: 'text', text: 'real' }, { type: 'text', text: SLASH_MODEL }] }],
    });
    expect(lastUserText(turns)).toBe('real');
  });
});

describe('본문 접기 — 들여쓰기·인라인 command-*', () => {
  it('/model 의 들여쓰기 태그 3개가 모두 블록으로 접히고, 이으면 원문 그대로(무손실)', () => {
    const text = `hi\n${SLASH_MODEL}\nhi`;
    const segs = splitInjectedBlocks(text);
    expect(segs.filter((s) => s.kind === 'block').map((s) => (s.kind === 'block' ? s.tag : ''))).toEqual([
      'command-name',
      'command-message',
      'command-args',
    ]);
    expect(segs.map((s) => s.text).join('')).toBe(text);
  });

  it('인라인 주입 태그(system-reminder/local-command-*/command-*)는 줄 중간에서도 접는다', () => {
    const text = 'see <local-command-stdout>out</local-command-stdout> and <command-x/> end';
    const segs = splitInjectedBlocks(text);
    expect(segs.map((s) => s.kind)).toEqual(['text', 'block', 'text', 'block', 'text']);
    expect(segs.map((s) => s.text).join('')).toBe(text);
  });

  it('사용자가 쓴 임의 인라인 태그는 본문에서 접지 않는다', () => {
    const text = 'make it <b>bold</b> please';
    expect(splitInjectedBlocks(text)).toEqual([{ kind: 'text', text }]);
  });
});

describe('긴 텍스트 접기 기준', () => {
  it(`${LONG_TEXT_MAX_LINES}줄·${LONG_TEXT_MAX_CHARS}자 이하면 그대로, 초과면 접힘`, () => {
    expect(isLongText('x\n'.repeat(LONG_TEXT_MAX_LINES - 1) + 'x')).toBe(false); // 40줄
    expect(isLongText('x\n'.repeat(LONG_TEXT_MAX_LINES) + 'x')).toBe(true); // 41줄
    expect(isLongText('x'.repeat(LONG_TEXT_MAX_CHARS))).toBe(false);
    expect(isLongText('x'.repeat(LONG_TEXT_MAX_CHARS + 1))).toBe(true);
  });

  it('미리보기는 앞 5줄, 한 줄짜리 거대 텍스트는 1,000자', () => {
    const lines = Array.from({ length: 100 }, (_, i) => `line ${i}`).join('\n');
    expect(longTextPreview(lines)).toBe('line 0\nline 1\nline 2\nline 3\nline 4…');
    expect(longTextPreview('y'.repeat(9000))).toHaveLength(1001);
  });

  it("'# Environment' 같은 긴 블록은 접혀서 렌더되고 토글로 전부 보인다", () => {
    const env = ['# Environment', ...Array.from({ length: 2000 }, (_, i) => `skill-${i}`)].join('\n');
    render(wrap(<LongText text={env} className="" />));
    expect(screen.queryByText(/skill-1999/)).toBeNull();
    const toggle = screen.getByRole('button', {
      name: `Show all (2,001 lines, ${env.length.toLocaleString('en-US')} chars)`,
    });
    fireEvent.click(toggle);
    expect(screen.getByText(/skill-1999/)).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Show less' })).toBeTruthy();
  });

  it('짧은 텍스트는 토글 없이 그대로', () => {
    render(wrap(<LongText text="hi" className="" />));
    expect(screen.getByText('hi')).toBeTruthy();
    expect(screen.queryByRole('button')).toBeNull();
  });
});

describe('세션 trace 셀', () => {
  it('상태 — 요청 상세와 같은 번역 라벨, 값이 없으면 —', () => {
    const { rerender } = render(wrap(<CallStatusBadge status="SUCCESS" />));
    expect(screen.getByText('OK')).toBeTruthy();
    rerender(wrap(<CallStatusBadge status="ERROR" />));
    expect(screen.getByText('Error')).toBeTruthy();
    rerender(wrap(<CallStatusBadge status="TIMEOUT" />));
    expect(screen.getByText('Timeout')).toBeTruthy();
    rerender(wrap(<CallStatusBadge status={null} />));
    expect(screen.getByText('—')).toBeTruthy();
  });

  it('캐시 R/W — 캐시 토큰이 없으면 —, 있으면 R/W 표기', () => {
    const { container, rerender } = render(wrap(<CacheCell read={0} write={null} />));
    expect(container.textContent).toBe('—');
    rerender(wrap(<CacheCell read={30000} write={12000} />));
    expect(container.textContent).not.toBe('—');
    expect(container.textContent).toMatch(/30K/);
  });
});
