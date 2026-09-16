// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * formatErrorDetail — 어떤 에러 본문에도 "[object Object]" 를 만들지 않는다.
 *
 * 회귀 배경: api-client 는 `detail` 을 문자열로 가정했지만 FastAPI/pydantic 의 기본
 * 422 는 `detail` 이 `[{loc,msg,type}, ...]` **배열**이다. 그 배열이 그대로 APIError
 * 메시지로 들어가 토스트에 "[object Object]" 만 떴고, 사용자는 어느 필드가 왜 틀렸는지
 * 알 수 없었다.
 */

import { describe, it, expect } from 'vitest';
import { formatErrorDetail } from '@/lib/utils/errorDetail';

describe('formatErrorDetail', () => {
  it('passes a plain string through', () => {
    expect(formatErrorDetail('Team not found')).toBe('Team not found');
  });

  it('returns undefined for null/undefined/empty string', () => {
    expect(formatErrorDetail(null)).toBeUndefined();
    expect(formatErrorDetail(undefined)).toBeUndefined();
    expect(formatErrorDetail('')).toBeUndefined();
  });

  it('folds a pydantic 422 detail array into "field: reason"', () => {
    const detail = [
      {
        type: 'literal_error',
        loc: ['query', 'group_by'],
        msg: "Input should be 'model', 'team' or 'user'",
      },
    ];
    const out = formatErrorDetail(detail);
    expect(out).toBe("group_by: Input should be 'model', 'team' or 'user'");
    expect(out).not.toContain('object Object');
  });

  it('joins multiple field errors with a separator', () => {
    const detail = [
      { loc: ['query', 'period'], msg: 'Field required' },
      { loc: ['body', 'price', 'input'], msg: 'Input should be a valid number' },
    ];
    expect(formatErrorDetail(detail)).toBe(
      'period: Field required; price.input: Input should be a valid number',
    );
  });

  it('never produces "[object Object]" for unexpected shapes', () => {
    const shapes: unknown[] = [
      [{ unexpected: 'shape' }],
      { msg: 'single object' },
      { message: 'alt key' },
      { deeply: { nested: true } },
      42,
      true,
      [],
    ];
    for (const shape of shapes) {
      const out = formatErrorDetail(shape);
      expect(out ?? '').not.toContain('object Object');
    }
  });

  it('keeps a loc that has no field path', () => {
    expect(formatErrorDetail([{ loc: ['body'], msg: 'Field required' }])).toBe(
      'body: Field required',
    );
  });
});
