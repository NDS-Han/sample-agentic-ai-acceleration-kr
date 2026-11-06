'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';

import type { BodyLogResponse } from '@/lib/actions/body-logs';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime, fmtTokensCompact, fmtUsd } from '@/lib/utils/format';

// ── 본문 텍스트 추출 ──────────────────────────────────────────────────────────
// request_body 방언: anthropic/openai chat = messages[], responses API = input.
// response_body: 비스트림은 JSON, 스트림은 {sse_text, truncated?} (SSE 프레임 원문).

interface ChatMessage {
  role?: string;
  type?: string;
  content?: unknown;
  name?: string;
  arguments?: unknown;
  output?: unknown;
  call_id?: string;
  summary?: unknown;
  [k: string]: unknown;
}

export function textFromContent(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((p) => {
        if (typeof p === 'string') return p;
        if (p && typeof p === 'object') {
          const o = p as Record<string, unknown>;
          // tool_result 는 사용자 발화가 아니라 툴 출력이다 — 문자열 content 도
          // 그대로 취하면 "마지막 user 메시지"가 툴 출력으로 오인된다.
          if (o.type === 'tool_result' || o.type === 'tool_use') return '';
          if (typeof o.text === 'string') return o.text;
          if (typeof o.content === 'string') return o.content;
        }
        return '';
      })
      .filter(Boolean)
      .join('\n');
  }
  return '';
}

function requestMessages(body: Record<string, unknown>): ChatMessage[] {
  const msgs = body.messages;
  if (Array.isArray(msgs)) return msgs as ChatMessage[];
  const input = body.input;
  if (typeof input === 'string') return [{ role: 'user', content: input }];
  if (Array.isArray(input)) return input as ChatMessage[];
  return [];
}

// ── 전체 대화 스레드 ──────────────────────────────────────────────────────────
// conversation 뷰는 마지막 턴이 아니라 모든 턴을 보여준다 — 요청 messages 안의
// 이전 어시스턴트/도구 호출까지 포함해서 에이전트 루프의 전체 맥락이 보인다.

export interface ToolUse {
  name: string;
  /** anthropic 은 객체, openai/responses 는 arguments 문자열. */
  input?: unknown;
}

export interface ToolResult {
  text: string;
  isError: boolean;
}

export interface ConversationTurn {
  role: string;
  text: string;
  tools: ToolUse[];
  results: ToolResult[];
  /** 원본 messages[]/input[] 의 인덱스 — historyStart 접기는 이 단위로 한다
      (빈 메시지가 턴에서 빠져도 메시지 수와 턴 수가 어긋나지 않게). */
  msgIndex: number;
}

function toolResultText(content: unknown): string {
  if (typeof content === 'string') return content;
  if (Array.isArray(content)) {
    return content
      .map((p) =>
        p && typeof p === 'object' && typeof (p as { text?: unknown }).text === 'string'
          ? (p as { text: string }).text
          : '',
      )
      .filter(Boolean)
      .join('\n');
  }
  return '';
}

/** tool_result 전문은 수십 KB 나온다 — 표시용은 잘라두고 전문은 원본 JSON 에 맡긴다. */
const TOOL_RESULT_DISPLAY_MAX = 4000;

function clippedToolResult(content: unknown): string {
  const text = toolResultText(content);
  return text.length > TOOL_RESULT_DISPLAY_MAX
    ? `${text.slice(0, TOOL_RESULT_DISPLAY_MAX)}…`
    : text;
}

function reasoningSummaryText(summary: unknown): string {
  if (typeof summary === 'string') return summary;
  if (Array.isArray(summary)) {
    return summary
      .map((s) =>
        s && typeof s === 'object' && typeof (s as { text?: unknown }).text === 'string'
          ? (s as { text: string }).text
          : '',
      )
      .filter(Boolean)
      .join('\n');
  }
  return '';
}

export function conversationTurns(body: Record<string, unknown>): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  requestMessages(body).forEach((m, msgIndex) => {
    // ── Responses API input[] 방언 — role/content 없는 타입 아이템을 정규화한다.
    // 이걸 걸러버리면 codex trace 에서 도구 호출이 통째로 안 보인다.
    const itemType = typeof m?.type === 'string' ? m.type : '';
    if (itemType === 'function_call' || itemType === 'custom_tool_call') {
      turns.push({
        role: 'assistant',
        text: '',
        // custom_tool_call 은 인자가 arguments 가 아니라 input 에 든다.
        tools: [
          {
            name: typeof m.name === 'string' ? m.name : itemType,
            input: m.arguments ?? m.input,
          },
        ],
        results: [],
        msgIndex,
      });
      return;
    }
    if (itemType === 'function_call_output' || itemType === 'custom_tool_call_output') {
      const text = toolResultText(m.output);
      turns.push({
        role: 'tool',
        text: '',
        tools: [],
        results: text
          ? [{ text: text.length > TOOL_RESULT_DISPLAY_MAX ? `${text.slice(0, TOOL_RESULT_DISPLAY_MAX)}…` : text, isError: false }]
          : [],
        msgIndex,
      });
      return;
    }
    if (itemType === 'reasoning') {
      const text = reasoningSummaryText(m.summary);
      if (text) turns.push({ role: 'reasoning', text, tools: [], results: [], msgIndex });
      return;
    }
    // web_search_call / local_shell_call / computer_call 등 무명 타입 — 보이게 둔다.
    // 인자 자리는 타입마다 다르다(arguments/input/action).
    if (itemType.endsWith('_call') && !m.role) {
      turns.push({
        role: 'tool',
        text: '',
        tools: [{ name: itemType, input: m.arguments ?? m.input ?? m.action }],
        results: [],
        msgIndex,
      });
      return;
    }

    // ── anthropic / openai chat 메시지 ────────────────────────────────────
    const text = textFromContent(m?.content);
    const tools: ToolUse[] = [];
    const results: ToolResult[] = [];
    if (Array.isArray(m?.content)) {
      for (const p of m.content) {
        if (!p || typeof p !== 'object') continue;
        const o = p as Record<string, unknown>;
        if (o.type === 'tool_use' && typeof o.name === 'string') {
          tools.push({ name: o.name, input: o.input });
        }
        if (o.type === 'tool_result') {
          const prev = clippedToolResult(o.content);
          if (prev) results.push({ text: prev, isError: o.is_error === true });
        }
      }
    }
    // openai chat 의 assistant.tool_calls — content 외 별도 필드.
    const tcs = m?.tool_calls;
    if (Array.isArray(tcs)) {
      for (const tc of tcs) {
        const fn = (tc as { function?: { name?: string; arguments?: string } }).function;
        if (fn?.name) tools.push({ name: fn.name, input: fn.arguments });
      }
    }
    if (text.trim() || tools.length || results.length) {
      turns.push({
        role: typeof m?.role === 'string' ? m.role : '?',
        text,
        tools,
        results,
        msgIndex,
      });
    }
  });
  return turns;
}

// ── system / instructions / tools 정의 ───────────────────────────────────────
// 대화 뷰에서 빠지면 안 되는 요청 컨텍스트 — system 프롬프트와 도구 정의가 응답을
// 좌우하므로 접힌 섹션으로 보여준다.

function systemTextOf(body: Record<string, unknown>): string {
  const sys = body.system;
  if (typeof sys === 'string') return sys;
  if (Array.isArray(sys)) {
    return sys
      .map((p) =>
        p && typeof p === 'object' && typeof (p as { text?: unknown }).text === 'string'
          ? (p as { text: string }).text
          : '',
      )
      .filter(Boolean)
      .join('\n');
  }
  const instr = body.instructions;
  return typeof instr === 'string' ? instr : '';
}

// ── 표시용 유니코드 이스케이프 디코드 ─────────────────────────────────────────
// provider 와이어(Bedrock SSE 등)가 ensure_ascii 직렬화라 sse_text/raw JSON 안에
// \uXXXX 리터럴이 남는다 — 표시 시점에만 디코드하고 저장 데이터는 건드리지 않는다.
// (?<!\\) — 진짜 백슬래시-u 리터럴(이스케이프된 \\u)은 건드리지 않는다.
// ⚠️ 이미 JSON.parse 된 텍스트(turn.text 등)에는 적용하지 않는다 — 사용자가
// 코드에 \uXXXX 를 문자 그대로 타이핑한 경우 내용이 바뀌어 보이기 때문이다.
// 적용 범위는 와이어 문자열(sse_text 원문, JSON.stringify 산출물)뿐이다.
const UNICODE_ESCAPE_RUN = /(?<!\\)((?:\\u[0-9a-fA-F]{4})+)/g;

export function decodeUnicodeEscapes(s: string): string {
  return s.replace(UNICODE_ESCAPE_RUN, (run) => {
    try {
      // 연속 시퀀스 통째로 JSON.parse — 서로게이트 쌍도 올바르게 합쳐진다.
      return JSON.parse(`"${run}"`) as string;
    } catch {
      return run;
    }
  });
}

// ── 응답 본문 추출 — 텍스트 + 도구 호출 ──────────────────────────────────────

export interface ResponseToolCall {
  name: string;
  /** 문자열이면 이미 직렬화된 인자(와이어 형태), 아니면 JSON.stringify 대상. */
  input: unknown;
}

export interface ResponseParts {
  text: string;
  toolCalls: ResponseToolCall[];
}

/** 이어 붙은 JSON 이벤트를 분리한다.
 *
 * /model/* passthrough 는 Converse/invoke 이벤트를 개행 없이 기록해
 * sse_text 가 `{…}{…}{…}` 한 줄이 된다 — split('\n') 만으로는 프레임이
 * 안 나뉜다. 문자열/이스케이프를 인식하며 중괄호 깊이를 세는 단일 패스로
 * 최상위 객체를 잘라낸다(data: 접두·공백·기타 잡음은 depth 0에서 무시).
 */
function splitJsonFrames(sse: string): string[] {
  const frames: string[] = [];
  let depth = 0;
  let start = -1;
  let inStr = false;
  let esc = false;
  for (let i = 0; i < sse.length; i++) {
    const c = sse[i];
    if (depth === 0) {
      if (c === '{') {
        depth = 1;
        start = i;
      }
      continue;
    }
    if (inStr) {
      if (esc) esc = false;
      else if (c === '\\') esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') inStr = true;
    else if (c === '{') depth++;
    else if (c === '}') {
      depth--;
      if (depth === 0) frames.push(sse.slice(start, i + 1));
    }
  }
  return frames;
}

/** SSE 프레임 원문에서 assistant 텍스트 + tool_use 블록을 재조립한다. */
export function extractSseParts(sse: string): ResponseParts {
  const parts: ResponseParts = { text: '', toolCalls: [] };
  // anthropic content_block 추적: index → 누적 중인 tool_use
  const blocks: Record<number, { name: string; inputJson: string }> = {};
  // openai tool_calls 추적: index → 누적
  const oaToolCalls: Record<number, { name: string; args: string }> = {};
  // responses function_call 추적: output_index → 누적
  const respCalls: Record<number, { name: string; args: string }> = {};

  const flush = (t: { name: string; args: string } | { name: string; inputJson: string }) => {
    const raw = 'args' in t ? t.args : t.inputJson;
    let input: unknown = raw;
    try {
      input = JSON.parse(raw);
    } catch {
      /* 미완성 JSON 인자는 원문 그대로 */
    }
    parts.toolCalls.push({ name: t.name, input });
  };

  // data: 프레임과 접두 없는 JSON 연결(/model/* passthrough)이 섞일 수 있으므로
  // 깊이 분리기로 프레임을 나눈다 — 한 줄에 여러 이벤트가 붙어 있어도 정확하다.
  for (const payload of splitJsonFrames(sse)) {
    if (!payload || payload === '[DONE]') continue;
    let ev: Record<string, unknown>;
    try {
      ev = JSON.parse(payload) as Record<string, unknown>;
    } catch {
      continue; // 파싱 불가 프레임은 건너뛴다
    }
    const type = ev.type as string | undefined;

    // Bedrock Converse 스트림(/model/* 경유) — contentBlock{Start,Delta,Stop}
    // 이벤트는 type 필드가 없고, contentBlockIndex 는 래퍼 안에 들어 있다.
    const inner = (ev.contentBlockStart ??
      ev.contentBlockDelta ??
      ev.contentBlockStop) as Record<string, unknown> | undefined;
    const cbIdx =
      typeof inner?.contentBlockIndex === 'number'
        ? inner.contentBlockIndex
        : typeof ev.contentBlockIndex === 'number'
          ? ev.contentBlockIndex
          : undefined;
    if (ev.contentBlockStart && cbIdx !== undefined) {
      const tu = (inner?.start as Record<string, unknown> | undefined)?.toolUse as
        | Record<string, unknown>
        | undefined;
      if (tu?.name) {
        blocks[cbIdx] = { name: String(tu.name), inputJson: '' };
      }
      continue;
    }
    if (ev.contentBlockDelta && cbIdx !== undefined) {
      const delta = inner?.delta as Record<string, unknown> | undefined;
      if (typeof delta?.text === 'string') parts.text += delta.text;
      const tu = delta?.toolUse as Record<string, unknown> | undefined;
      if (typeof tu?.input === 'string' && blocks[cbIdx]) {
        blocks[cbIdx].inputJson += tu.input;
      }
      continue;
    }
    if (ev.contentBlockStop && cbIdx !== undefined) {
      const b = blocks[cbIdx];
      if (b) {
        flush({ name: b.name, inputJson: b.inputJson });
        delete blocks[cbIdx];
      }
      continue;
    }

    // anthropic: content_block 생애주기
    if (type === 'content_block_start') {
      const cb = ev.content_block as Record<string, unknown> | undefined;
      if (cb?.type === 'tool_use' && typeof ev.index === 'number') {
        blocks[ev.index] = { name: String(cb.name ?? ''), inputJson: '' };
      }
      continue;
    }
    if (type === 'content_block_delta') {
      const delta = ev.delta as Record<string, unknown> | undefined;
      if (delta?.type === 'input_json_delta' && typeof ev.index === 'number' && blocks[ev.index]) {
        blocks[ev.index].inputJson += String(delta.partial_json ?? '');
        continue;
      }
      if (typeof delta?.text === 'string') parts.text += delta.text;
      continue;
    }
    if (type === 'content_block_stop' && typeof ev.index === 'number') {
      const b = blocks[ev.index];
      if (b) {
        flush({ name: b.name, inputJson: b.inputJson });
        delete blocks[ev.index];
      }
      continue;
    }

    // responses API — function_call 은 arguments, custom_tool_call 은 input 에
    // 인자가 든다. done 아이템은 누적 델타보다 완전한 최종본을 우선 쓴다.
    if (type === 'response.output_item.added') {
      const item = ev.item as Record<string, unknown> | undefined;
      const it = item?.type;
      if (
        item != null &&
        (it === 'function_call' || it === 'custom_tool_call') &&
        typeof ev.output_index === 'number'
      ) {
        respCalls[ev.output_index] = {
          name: String(item.name ?? ''),
          args: typeof item.arguments === 'string'
            ? item.arguments
            : typeof item.input === 'string'
              ? item.input
              : '',
        };
      }
      continue;
    }
    if (
      (type === 'response.function_call_arguments.delta' ||
        type === 'response.custom_tool_call_input.delta') &&
      typeof ev.output_index === 'number'
    ) {
      const rc = respCalls[ev.output_index] ?? (respCalls[ev.output_index] = { name: '', args: '' });
      rc.args += String(ev.delta ?? '');
      continue;
    }
    if (type === 'response.output_item.done') {
      const item = ev.item as Record<string, unknown> | undefined;
      const it = item?.type;
      if (
        item != null &&
        (it === 'function_call' || it === 'custom_tool_call') &&
        typeof ev.output_index === 'number'
      ) {
        const rc = respCalls[ev.output_index];
        const finalArgs =
          typeof item.arguments === 'string' && item.arguments
            ? item.arguments
            : typeof item.input === 'string' && item.input
              ? item.input
              : '';
        if (finalArgs) {
          flush({ name: rc?.name || String(item.name ?? ''), args: finalArgs });
        } else if (rc) {
          flush(rc);
        }
        delete respCalls[ev.output_index];
      }
      continue;
    }
    if (type === 'response.output_text.delta' && typeof ev.delta === 'string') {
      parts.text += ev.delta;
      continue;
    }

    // openai chat
    const choices = ev.choices as { delta?: Record<string, unknown> }[] | undefined;
    const d = choices?.[0]?.delta;
    if (d) {
      if (typeof d.content === 'string') parts.text += d.content;
      const tcs = d.tool_calls as
        | { index?: number; function?: { name?: string; arguments?: string } }[]
        | undefined;
      if (Array.isArray(tcs)) {
        for (const tc of tcs) {
          const idx = tc.index ?? 0;
          const acc = oaToolCalls[idx] ?? (oaToolCalls[idx] = { name: '', args: '' });
          if (tc.function?.name) acc.name = tc.function.name;
          if (tc.function?.arguments) acc.args += tc.function.arguments;
        }
      }
    }
  }
  // 스트림이 중간에 끊겨도 누적분은 보여준다.
  for (const b of Object.values(blocks)) flush({ name: b.name, inputJson: b.inputJson });
  for (const t of Object.values(oaToolCalls)) if (t.name || t.args) flush(t);
  for (const t of Object.values(respCalls)) if (t.name || t.args) flush(t);
  return parts;
}

export function nonstreamResponseParts(body: Record<string, unknown>): ResponseParts {
  const parts: ResponseParts = { text: '', toolCalls: [] };
  const content = body.content;
  if (Array.isArray(content)) {
    // anthropic — text 블록과 tool_use 블록이 섞여 있다
    parts.text = textFromContent(content);
    for (const p of content) {
      const o = p as Record<string, unknown>;
      if (o?.type === 'tool_use' && typeof o.name === 'string') {
        parts.toolCalls.push({ name: o.name, input: o.input });
      }
    }
    return parts;
  }
  const choices = body.choices as
    | { message?: { content?: unknown; tool_calls?: { function?: { name?: string; arguments?: string } }[] } }[]
    | undefined;
  const msg = choices?.[0]?.message;
  if (msg) {
    if (msg.content) parts.text = textFromContent(msg.content);
    for (const tc of msg.tool_calls ?? []) {
      if (tc.function?.name) {
        parts.toolCalls.push({ name: tc.function.name, input: tc.function.arguments });
      }
    }
    return parts;
  }
  if (typeof body.output_text === 'string') {
    parts.text = body.output_text; // responses API 편의 필드
    return parts;
  }
  // responses API 원본: output[] 의 message/output_text/function_call 아이템
  const output = body.output;
  if (Array.isArray(output)) {
    for (const item of output) {
      const o = item as Record<string, unknown>;
      if (o?.type === 'message' && Array.isArray(o.content)) {
        const t = textFromContent(o.content);
        if (t) parts.text = t;
      }
      if (o?.type === 'output_text' && typeof o.text === 'string') parts.text = o.text;
      if (o?.type === 'function_call' && typeof o.name === 'string') {
        parts.toolCalls.push({ name: o.name, input: o.arguments });
      }
      if (o?.type === 'custom_tool_call' && typeof o.name === 'string') {
        parts.toolCalls.push({ name: o.name, input: o.input });
      }
    }
  }
  return parts;
}

export function statusTone(status: string): BadgeTone {
  if (status === 'ERROR' || status === 'error') return 'pink';
  if (status === 'TIMEOUT' || status === 'partial') return 'amber';
  return 'teal';
}

export function JsonBlock({ value }: { value: unknown }) {
  // 디코드하지 않는다 — stringify 가 문자열 안의 \ 를 \\ 로 이스케이프해서
  // \uXXXX 는 \\uXXXX 가 되고, lookbehind 를 우회해 디코드하면 사용자 원문이
  // 바뀐다. 와이어 이스케이프가 실제로 있는 sse_text 는 호출부가 값만 디코드해
  // 넣는다(displayResBody).
  return (
    <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-all">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

// ── 접힌 섹션 — <details> 는 접혀 있어도 children 이 렌더돼서, 무거운 내용은 ──
// useState + 조건부 렌더로 열릴 때만 그린다.

function Collapsible({
  label,
  children,
}: {
  label: React.ReactNode;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(false);
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="text-xs text-muted-foreground hover:text-foreground"
      >
        {open ? '▾ ' : '▸ '}
        {label}
      </button>
      {open ? children : null}
    </div>
  );
}

// 와이어 인자(openai arguments, anthropic input_json_delta 누적)는 문자열 —
// 그 안의 \uXXXX 는 provider 의 ensure_ascii 흔적이라 디코드한다. 이미 파싱된
// 객체면 stringify 결과물이라 사용자 원문 보존을 위해 디코드하지 않는다.
function toolInputJson(input: unknown): string {
  if (typeof input === 'string') return decodeUnicodeEscapes(input);
  try {
    return JSON.stringify(input, null, 2);
  } catch {
    return String(input);
  }
}

// ── 공유 본문 뷰 — 다이얼로그(단건)와 세션 trace 카드(인라인)가 같이 쓴다 ──────

export function BodyRecordContent({
  data,
  historyStart = 0,
}: {
  data: BodyLogResponse;
  /** 세션 trace 에서 직전 호출까지의 턴 수 — 앞의 N턴은 접어 둔다(에이전트 루프는
      요청마다 전체 history 를 싣기 때문에 누적 맥락이 반복된다). */
  historyStart?: number;
}) {
  const t = useTranslations('monitoring.bodyLogs');
  const locale = useLocale();
  const tz = useReportingTz();
  const [rawReq, setRawReq] = useState(false);
  const [rawRes, setRawRes] = useState(false);
  const [rawReqFull, setRawReqFull] = useState(false);
  const [showAllTurns, setShowAllTurns] = useState(false);
  const rec = data.record;

  const reqBody = (rec?.request_body ?? {}) as Record<string, unknown>;
  const resBody = (rec?.response_body ?? {}) as Record<string, unknown>;
  const sseText = typeof resBody.sse_text === 'string' ? (resBody.sse_text as string) : '';
  const truncated = resBody.truncated === true;
  // raw JSON 뷰에서 sse_text 의 와이어 이스케이프(\uXXXX)만 읽기 좋게 디코드 —
  // 레코드의 다른 문자열은 이미 파싱된 원문이라 건드리면 사용자 입력이 바뀐다.
  const displayResBody = sseText
    ? { ...resBody, sse_text: decodeUnicodeEscapes(sseText) }
    : resBody;

  const resParts = sseText ? extractSseParts(sseText) : nonstreamResponseParts(resBody);
  const unparseable = reqBody._unparseable === true;
  const rawWrapped = '_raw' in reqBody;

  const allTurns = conversationTurns(reqBody);
  // historyStart 는 "직전 호출까지의 원본 메시지 수" — msgIndex 기준으로 접는다.
  // 빈 메시지가 턴에서 빠져도 단위가 어긋나지 않고, 호출부는 접두가 실제로
  // 이어지는지 검증한 값만 넘긴다(아니면 0).
  const hiddenTurns =
    !showAllTurns && historyStart > 0
      ? allTurns.filter((turn) => turn.msgIndex < historyStart).length
      : 0;
  const turns = allTurns.slice(hiddenTurns);

  const sysText = systemTextOf(reqBody);
  const toolsDef = reqBody.tools;

  return (
    <div>
      {/* 메타 */}
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge tone={statusTone(rec?.status ?? data.record?.status ?? '')}>{rec?.status ?? '-'}</Badge>
        <span>{rec?.model_alias ?? data.model_alias}</span>
        <span>·</span>
        <span>{data.requested_at ? fmtDateTime(data.requested_at, locale, tz) : '-'}</span>
        <span>·</span>
        <span className="font-mono">{data.request_id}</span>
        {(data.user_email ?? rec?.user_id) && (
          <>
            <span>·</span>
            <span title={rec?.user_id ?? undefined}>{data.user_email ?? rec?.user_id}</span>
          </>
        )}
        {data.client && (
          <>
            <span>·</span>
            <Badge tone="neutral">{data.client}</Badge>
          </>
        )}
        {rec?.is_streaming && <Badge tone="neutral">{t('streamTag')}</Badge>}
        {data.latency_ms != null && (
          <span>{t('latencyMs', { ms: data.latency_ms })}</span>
        )}
        {data.ttft_ms != null && <span>{t('ttftTag', { ms: data.ttft_ms })}</span>}
        {data.cost_usd != null && <span>{fmtUsd(data.cost_usd)}</span>}
        {data.input_tokens != null && (
          <span>
            {t('tokensIn', { n: fmtTokensCompact(data.input_tokens) })}
            {data.output_tokens != null
              ? ` / ${t('tokensOut', { n: fmtTokensCompact(data.output_tokens) })}`
              : ''}
          </span>
        )}
        {truncated && <Badge tone="amber">{t('truncated')}</Badge>}
        {data.oversized && <Badge tone="amber">{t('oversized')}</Badge>}
        {data.incomplete && <Badge tone="amber">{t('scanIncomplete')}</Badge>}
        {data.duplicates > 0 && (
          <Badge tone="neutral">{t('duplicates', { count: data.duplicates })}</Badge>
        )}
      </div>

      {data.oversized && (
        <p className="mb-4 text-xs text-amber-600 dark:text-amber-400">{t('oversizedHint')}</p>
      )}

      <div className="space-y-4">
        {/* 요청 본문 */}
        <section>
          <div className="mb-1 flex items-center justify-between">
            <h3 className="text-sm font-semibold">{t('requestBody')}</h3>
            <button
              type="button"
              onClick={() => setRawReq((v) => !v)}
              className="text-xs text-primary hover:underline"
            >
              {rawReq ? t('showConversation') : t('showRaw')}
            </button>
          </div>
          {unparseable && <p className="mb-1 text-xs text-amber-600">{t('unparseable')}</p>}
          {rawWrapped && !unparseable && (
            <p className="mb-1 text-xs text-amber-600">{t('rawWrapped')}</p>
          )}
          {rawReq ? (
            <JsonBlock value={reqBody} />
          ) : (
            <>
              {/* system 프롬프트 — 응답을 좌우하는 컨텍스트라 빠뜨리면 안 된다 */}
              {sysText ? (
                <Collapsible label={t('systemPrompt')}>
                  <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border p-2 text-xs">
                    {sysText}
                  </pre>
                </Collapsible>
              ) : null}
              {Array.isArray(toolsDef) && toolsDef.length > 0 ? (
                <Collapsible
                  label={t('toolsDefined', { count: toolsDef.length })}
                >
                  <JsonBlock value={toolsDef} />
                </Collapsible>
              ) : null}
              {turns.length ? (
                <div className="max-h-96 space-y-3 overflow-auto rounded-md border border-border p-3">
                  {hiddenTurns > 0 && (
                    <button
                      type="button"
                      onClick={() => setShowAllTurns(true)}
                      className="text-xs text-primary hover:underline"
                    >
                      {t('showEarlierTurns', { count: hiddenTurns })}
                    </button>
                  )}
                  {turns.map((turn, i) => (
                    <div key={i}>
                      <p
                        className={`mb-0.5 text-[11px] font-medium uppercase tracking-wide ${
                          turn.role === 'assistant'
                            ? 'text-teal-600 dark:text-teal-400'
                            : turn.role === 'reasoning' || turn.role === 'tool'
                              ? 'text-violet-600 dark:text-violet-400'
                              : 'text-muted-foreground'
                        }`}
                      >
                        {turn.role}
                      </p>
                      {turn.text ? (
                        <pre className="whitespace-pre-wrap break-words text-sm">
                          {turn.text}
                        </pre>
                      ) : null}
                      {turn.tools.map((tu, j) => (
                        <Collapsible
                          key={j}
                          label={`⚙ ${t('toolCall', { name: tu.name })}`}
                        >
                          {tu.input != null ? (
                            <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-2 text-xs">
                              {toolInputJson(tu.input)}
                            </pre>
                          ) : null}
                        </Collapsible>
                      ))}
                      {turn.results.map((r, j) => (
                        <Collapsible
                          key={j}
                          label={
                            <>
                              {t('toolResult')}
                              {r.isError ? (
                                <span className="text-destructive">{t('toolResultError')}</span>
                              ) : null}
                            </>
                          }
                        >
                          <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 text-xs italic text-muted-foreground">
                            {r.text}
                          </pre>
                        </Collapsible>
                      ))}
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">{t('noUserMessage')}</p>
              )}
              {/* <details> 는 접혀 있어도 children 이 렌더된다 — 수 MB 요청을
                  매 렌더 stringify 하지 않도록 열렸을 때만 그린다. */}
              {rawReqFull ? (
                <>
                  <button
                    type="button"
                    onClick={() => setRawReqFull(false)}
                    className="mt-2 text-xs text-muted-foreground hover:text-foreground"
                  >
                    {t('hideFullJson')}
                  </button>
                  <JsonBlock value={reqBody} />
                </>
              ) : (
                <button
                  type="button"
                  onClick={() => setRawReqFull(true)}
                  className="mt-2 text-xs text-muted-foreground hover:text-foreground"
                >
                  {t('fullRequestJson')}
                </button>
              )}
            </>
          )}
        </section>

        {/* 응답 본문 */}
        <section>
          <div className="mb-1 flex items-center justify-between">
            <h3 className="text-sm font-semibold">{t('responseBody')}</h3>
            <button
              type="button"
              onClick={() => setRawRes((v) => !v)}
              className="text-xs text-primary hover:underline"
            >
              {rawRes ? t('showConversation') : t('showRaw')}
            </button>
          </div>
          {rawRes ? (
            <JsonBlock value={displayResBody} />
          ) : resParts.text || resParts.toolCalls.length ? (
            <div className="max-h-80 space-y-2 overflow-auto rounded-md border border-border p-3">
              {resParts.text ? (
                <pre className="whitespace-pre-wrap break-words text-sm">{resParts.text}</pre>
              ) : null}
              {resParts.toolCalls.map((tc, j) => (
                <Collapsible key={j} label={`⚙ ${t('toolCall', { name: tc.name })}`}>
                  {tc.input != null ? (
                    <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-md bg-muted p-2 text-xs">
                      {toolInputJson(tc.input)}
                    </pre>
                  ) : null}
                </Collapsible>
              ))}
            </div>
          ) : sseText ? (
            // 델타 추출 실패 — SSE 원문 노출 (와이어의 \uXXXX 는 표시용 디코드)
            <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap break-all">
              {decodeUnicodeEscapes(sseText)}
            </pre>
          ) : (
            <JsonBlock value={displayResBody} />
          )}
        </section>

        {rec?.error ? (
          <section>
            <h3 className="mb-1 text-sm font-semibold text-destructive">{t('errorSection')}</h3>
            <JsonBlock value={rec.error} />
          </section>
        ) : null}
      </div>
    </div>
  );
}
