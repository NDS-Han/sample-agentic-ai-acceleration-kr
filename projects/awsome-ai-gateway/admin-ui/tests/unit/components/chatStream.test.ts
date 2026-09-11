// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Regression: SSE 프레임 파싱 + admin-api ↔ admin-ui 이벤트 계약.
 *
 * ⚠️ 고친 결함: `parseSseBlock` 이 프레임을 `{ type: event, ...parsed }` 로 합쳐서
 * **payload 의 `type` 이 SSE 이벤트 이름을 덮어썼다.** admin-api 의 에러 프레임 본문이
 * `{"error": …, "type": "<예외클래스명>"}` 였기 때문에 최종 이벤트가 `type: 'ClientError'`
 * 가 되어, ChatLayout 의 `case 'error'` 가 **한 번도** 매칭되지 않고 `default: return msg`
 * 로 조용히 버려졌다 — AgentCore 호출이 실패해도 사용자 화면엔 아무 메시지 없이 pending
 * 스피너만 영구히 돌았다. (백엔드도 본문 키를 `error_type` 으로 바꿨다 — 이중 방어.)
 *
 * 뒤쪽 계약 테스트는 백엔드 소스를 직접 읽어 **양쪽이 같은 이벤트 이름 집합을 쓰는지**
 * 검사한다(`plan` 이 유니온에서 빠져 있던 것도 이 부류였다).
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve, basename } from 'node:path';
import { parseSseBlock } from '@/components/chat/useChatStream';
import type { StreamEvent } from '@/components/chat/types';

/** 블록 하나를 넣고 방출된 이벤트 배열을 받는다. */
function collect(block: string): StreamEvent[] {
  const out: StreamEvent[] = [];
  parseSseBlock(block, (e) => out.push(e));
  return out;
}

// vitest 는 admin-ui/ 를 cwd 로 돌린다. `import.meta.url` 은 vite 가 `/@fs/…` 로
// 다시 써서 fs 로 못 읽는다 — cwd 기준으로 레포 루트를 잡는다.
function repoRoot(): string {
  const cwd = process.cwd();
  if (basename(cwd) !== 'admin-ui') {
    throw new Error(`cwd 가 admin-ui 가 아니다(${cwd}) — admin-ui 에서 vitest 를 돌려야 한다`);
  }
  return resolve(cwd, '..');
}

function readRepoFile(rel: string): string {
  // 경로가 틀리면 아래 계약 테스트가 전부 공허해지므로 조용히 skip 하지 않고 던진다.
  const src = readFileSync(resolve(repoRoot(), rel), 'utf-8');
  if (!src.trim()) throw new Error(`${rel} 이 비어 있다 — 계약 테스트가 공허해진다`);
  return src;
}

// ─────────────────────────────────────────────────────────────────────────────
// 0) 하네스 공허성 대조군
// ─────────────────────────────────────────────────────────────────────────────

describe('parseSseBlock 하네스', () => {
  it('평범한 프레임을 실제로 방출한다(대조군)', () => {
    const events = collect('event: text\ndata: {"chunk":"안녕"}');
    expect(events).toHaveLength(1);
    expect(events[0]).toEqual({ type: 'text', chunk: '안녕' });
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 1) 결함 본체 — event: 이름이 payload 의 type 보다 우선한다
// ─────────────────────────────────────────────────────────────────────────────

describe('event: 이름 우선순위', () => {
  it('백엔드 에러 프레임이 ChatLayout 의 error 분기로 라우팅된다', () => {
    // admin-api `_publish_error` 가 실제로 내보내는 형태.
    const events = collect(
      'event: error\ndata: {"error":"자격증명 없음","error_type":"ClientError"}'
    );
    expect(events).toHaveLength(1);
    // 이게 'ClientError' 면 case 'error' 가 안 잡히고 스피너가 영구히 돈다.
    expect(events[0].type).toBe('error');
    expect(events[0]).toMatchObject({ error: '자격증명 없음', error_type: 'ClientError' });
  });

  it('payload 의 type 은 event: 이름을 절대 덮어쓰지 못한다(옛 결함 회귀)', () => {
    // 옛 백엔드가 보내던 형태 — 본문 키가 `type` 이다. 파서만으로도 막혀야 한다.
    const events = collect('event: error\ndata: {"error":"boom","type":"ClientError"}');
    expect(events[0].type).toBe('error');
  });

  it('에러 이외의 프레임에서도 같은 규칙이 적용된다', () => {
    const events = collect('event: chart\ndata: {"spec":{"kind":"bar"},"type":"text"}');
    expect(events[0].type).toBe('chart');
  });

  it('옛 병합 순서는 실제로 결함을 냈다(음성 대조군)', () => {
    // 예전 구현: `{ type: event, ...parsed }` — payload 가 나중이라 이름이 덮인다.
    //
    // ⚠️ 리터럴 스프레드(`{ type: 'error', ...body }`)로 쓰면 tsc 가 TS2783
    //    ("'type' is specified more than once") 을 낸다 — 그 경고가 바로 이 결함의
    //    정체다. 경고를 억제(@ts-ignore)하는 대신 Object.assign 으로 같은 런타임
    //    의미를 재현한다. 그래야 프로덕션 코드에서 같은 실수를 하면 tsc 가 계속 잡는다.
    const body = { error: 'boom', type: 'ClientError' };
    const oldMerged = Object.assign({ type: 'error' }, body);
    expect(oldMerged.type).toBe('ClientError');
    expect(oldMerged.type).not.toBe('error');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 2) event: 줄이 없는 프레임(AgentCore 직결) 은 payload 의 type 을 쓴다
// ─────────────────────────────────────────────────────────────────────────────

describe('event: 줄이 없는 프레임', () => {
  it('payload 의 type 으로 폴백한다', () => {
    // BedrockAgentCoreApp 은 `data: <json>` 만 내보낸다(event: 줄 없음).
    const events = collect('data: {"type":"thinking","text":"분석 중"}');
    expect(events[0]).toEqual({ type: 'thinking', text: '분석 중' });
  });

  it('type 도 없으면 message 로 떨어진다', () => {
    const events = collect('data: {"foo":1}');
    expect(events[0].type).toBe('message');
  });

  it('type 이 문자열이 아니면 message 로 떨어진다', () => {
    const events = collect('data: {"type":42}');
    expect(events[0].type).toBe('message');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 3) 방어적 파싱
// ─────────────────────────────────────────────────────────────────────────────

describe('방어적 파싱', () => {
  it('JSON 이 아니면 텍스트로 취급한다', () => {
    const events = collect('event: text\ndata: 그냥 문자열');
    expect(events[0]).toEqual({ type: 'text', chunk: '그냥 문자열' });
  });

  it('비객체 JSON 을 스프레드해 문자 인덱스로 흩뿌리지 않는다', () => {
    // `{...'hi'}` → `{0:'h',1:'i'}` 가 되던 함정.
    const events = collect('data: "hi"');
    expect(events[0]).toEqual({ type: 'text', chunk: 'hi' });
    expect(events[0]).not.toHaveProperty('0');
  });

  it('null payload 도 흩뿌리지 않는다', () => {
    const events = collect('data: null');
    expect(events[0]).toEqual({ type: 'text', chunk: 'null' });
  });

  it('data 가 없는 블록은 아무것도 방출하지 않는다', () => {
    expect(collect('event: done')).toHaveLength(0);
  });

  it('keepalive 코멘트 블록은 무시된다', () => {
    // `_StreamRelay.tail()` 이 주기적으로 `: keepalive` 를 흘린다.
    expect(collect(': keepalive')).toHaveLength(0);
  });

  it('여러 data: 줄은 이어붙인다', () => {
    const events = collect('event: text\ndata: {"chunk":\ndata: "쪼개진 JSON"}');
    expect(events[0]).toEqual({ type: 'text', chunk: '쪼개진 JSON' });
  });

  it('CRLF 프레임도 처리한다', () => {
    // 프록시가 개행을 바꿔 보내는 경우.
    const events = collect('event: text\r\ndata: {"chunk":"x"}\r');
    expect(events[0].type).toBe('text');
  });
});

// ─────────────────────────────────────────────────────────────────────────────
// 4) 백엔드 ↔ 프론트엔드 이벤트 계약 (소스 대조)
// ─────────────────────────────────────────────────────────────────────────────

describe('admin-api ↔ admin-ui 이벤트 계약', () => {
  const backend = readRepoFile('admin-api/src/app/routers/chat_agent.py');
  const chatLayout = readRepoFile('admin-ui/src/components/chat/ChatLayout.tsx');
  const types = readRepoFile('admin-ui/src/components/chat/types.ts');

  // `_sse("name", …)` — heartbeat 처럼 여러 줄로 쪼개진 호출도 잡는다.
  const emitted = [...backend.matchAll(/_sse\(\s*"([a-z_]+)"/g)].map((m) => m[1]);
  const uniqueEmitted = [...new Set(emitted)].sort();
  const handled = new Set([...chatLayout.matchAll(/case '([a-z_]+)':/g)].map((m) => m[1]));
  const declared = new Set([...types.matchAll(/type: '([a-z_]+)'/g)].map((m) => m[1]));

  it('추출이 실제로 이벤트 이름을 찾았다(대조군)', () => {
    // 헬퍼 이름이나 switch 형태가 바뀌면 아래 두 단정이 공허해진다.
    expect(uniqueEmitted.length).toBeGreaterThanOrEqual(12);
    expect(handled.size).toBeGreaterThanOrEqual(12);
    expect(declared.size).toBeGreaterThanOrEqual(12);
    expect(uniqueEmitted).toContain('error');
    expect(uniqueEmitted).toContain('heartbeat'); // 여러 줄 호출 — 정규식이 놓치기 쉽다
  });

  it('백엔드가 보내는 모든 이벤트를 ChatLayout 이 처리한다', () => {
    const unhandled = uniqueEmitted.filter((n) => !handled.has(n));
    expect(unhandled, `applyEvent 의 default 로 조용히 버려지는 이벤트: ${unhandled}`).toEqual(
      []
    );
  });

  it('백엔드가 보내는 모든 이벤트가 StreamEvent 유니온에 있다', () => {
    const undeclared = uniqueEmitted.filter((n) => !declared.has(n));
    expect(
      undeclared,
      `StreamEvent 유니온에 없는 이벤트(applyEvent 가 event: any 라 타입검사에 안 걸린다): ${undeclared}`
    ).toEqual([]);
  });

  it('에러 본문 키는 error_type 이다 — type 이면 이벤트 이름을 덮는다', () => {
    expect(backend).toContain('_sse("error", {"error": message, "error_type": error_type})');
    expect(backend).not.toMatch(/_sse\(\s*"error",\s*\{[^}]*"type":/);
  });
});
