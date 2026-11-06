'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { useLocale, useTranslations } from 'next-intl';

import type { BodyLogResponse } from '@/lib/actions/body-logs';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime } from '@/lib/utils/format';

// ── 본문 텍스트 추출 ──────────────────────────────────────────────────────────
// request_body 방언: anthropic/openai chat = messages[], responses API = input.
// response_body: 비스트림은 JSON, 스트림은 {sse_text, truncated?} (SSE 프레임 원문).

interface ChatMessage {
  role?: string;
  content?: unknown;
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

export function lastUserText(body: Record<string, unknown>): string {
  const msgs = requestMessages(body);
  for (let i = msgs.length - 1; i >= 0; i--) {
    if (msgs[i]?.role !== 'user') continue;
    // Claude Code 의 마지막 user 턴은 대부분 tool_result 블록 — 텍스트가 비면
    // 건너뛰고 더 이전 user 메시지를 찾는다.
    const text = textFromContent(msgs[i].content);
    if (text.trim()) return text;
  }
  return '';
}

// ── 전체 대화 스레드 ──────────────────────────────────────────────────────────
// conversation 뷰는 마지막 턴이 아니라 모든 턴을 보여준다 — 요청 messages 안의
// 이전 어시스턴트/도구 호출까지 포함해서 에이전트 루프의 전체 맥락이 보인다.

export interface ConversationTurn {
  role: string;
  text: string;
  tools: string[]; // tool_use 이름들
  results: string[]; // tool_result 미리보기 (절단)
}

function toolResultPreview(content: unknown): string {
  const text =
    typeof content === 'string'
      ? content
      : Array.isArray(content)
        ? content
            .map((p) =>
              p && typeof p === 'object' && typeof (p as { text?: unknown }).text === 'string'
                ? ((p as { text: string }).text)
                : '',
            )
            .filter(Boolean)
            .join('\n')
        : '';
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
}

export function conversationTurns(body: Record<string, unknown>): ConversationTurn[] {
  const turns: ConversationTurn[] = [];
  for (const m of requestMessages(body)) {
    const text = textFromContent(m?.content);
    const tools: string[] = [];
    const results: string[] = [];
    if (Array.isArray(m?.content)) {
      for (const p of m.content) {
        if (!p || typeof p !== 'object') continue;
        const o = p as Record<string, unknown>;
        if (o.type === 'tool_use' && typeof o.name === 'string') tools.push(o.name);
        if (o.type === 'tool_result') {
          const prev = toolResultPreview(o.content);
          if (prev) results.push(prev);
        }
      }
    }
    if (text.trim() || tools.length || results.length) {
      turns.push({ role: typeof m?.role === 'string' ? m.role : '?', text, tools, results });
    }
  }
  return turns;
}

// ── 표시용 유니코드 이스케이프 디코드 ─────────────────────────────────────────
// provider 와이어(Bedrock SSE 등)가 ensure_ascii 직렬화라 sse_text/raw JSON 안에
// \uXXXX 리터럴이 남는다 — 표시 시점에만 디코드하고 저장 데이터는 건드리지 않는다.
// (?<!\\) — 진짜 백슬래시-u 리터럴(이스케이프된 \\u)은 건드리지 않는다.
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

/** SSE 프레임 원문에서 assistant 텍스트 델타를 추출 — anthropic / openai / responses 방언. */
export function extractSseText(sse: string): string {
  const parts: string[] = [];
  for (const line of sse.split('\n')) {
    if (!line.startsWith('data:')) continue;
    const payload = line.slice(5).trim();
    if (!payload || payload === '[DONE]') continue;
    try {
      const ev = JSON.parse(payload) as Record<string, unknown>;
      // anthropic: content_block_delta → delta.text
      const delta = ev.delta as Record<string, unknown> | undefined;
      if (typeof delta?.text === 'string') parts.push(delta.text);
      // responses API: output_text.delta 만 — function_call_arguments/reasoning
      // 요약 등 문자열 델타까지 이어 붙이면 툴 인자가 답변으로 보인다.
      else if (ev.type === 'response.output_text.delta' && typeof ev.delta === 'string')
        parts.push(ev.delta);
      // openai chat: choices[0].delta.content
      const choices = ev.choices as { delta?: { content?: string } }[] | undefined;
      if (choices?.[0]?.delta?.content) parts.push(choices[0].delta.content);
    } catch {
      /* 파싱 불가 프레임은 건너뛴다 */
    }
  }
  return parts.join('');
}

export function nonstreamResponseText(body: Record<string, unknown>): string {
  const content = body.content;
  if (Array.isArray(content)) return textFromContent(content); // anthropic
  const choices = body.choices as { message?: { content?: unknown } }[] | undefined;
  if (choices?.[0]?.message?.content) return textFromContent(choices[0].message.content);
  if (typeof body.output_text === 'string') return body.output_text; // responses API 편의 필드
  // responses API 원본: output[] 의 message/output_text 아이템
  const output = body.output;
  if (Array.isArray(output)) {
    for (const item of output) {
      const o = item as Record<string, unknown>;
      if (o?.type === 'message' && Array.isArray(o.content)) {
        const t = textFromContent(o.content);
        if (t) return t;
      }
      if (o?.type === 'output_text' && typeof o.text === 'string') return o.text;
    }
  }
  return '';
}

export function statusTone(status: string): BadgeTone {
  if (status === 'error') return 'pink';
  if (status === 'partial') return 'amber';
  return 'teal';
}

export function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-all">
      {decodeUnicodeEscapes(JSON.stringify(value, null, 2))}
    </pre>
  );
}

// ── 공유 본문 뷰 — 다이얼로그(단건)와 세션 trace 카드(인라인)가 같이 쓴다 ──────

export function BodyRecordContent({ data }: { data: BodyLogResponse }) {
  const t = useTranslations('monitoring.bodyLogs');
  const locale = useLocale();
  const tz = useReportingTz();
  const [rawReq, setRawReq] = useState(false);
  const [rawRes, setRawRes] = useState(false);
  const [rawReqFull, setRawReqFull] = useState(false);
  const rec = data.record;

  const reqBody = (rec?.request_body ?? {}) as Record<string, unknown>;
  const resBody = (rec?.response_body ?? {}) as Record<string, unknown>;
  const sseText = typeof resBody.sse_text === 'string' ? (resBody.sse_text as string) : '';
  const truncated = resBody.truncated === true;

  const resText = sseText ? extractSseText(sseText) : nonstreamResponseText(resBody);
  const unparseable = reqBody._unparseable === true;
  const rawWrapped = '_raw' in reqBody;

  return (
    <div>
      {/* 메타 */}
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge tone={statusTone(rec?.status ?? '')}>{rec?.status ?? '-'}</Badge>
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
        {rec?.is_streaming && <Badge tone="neutral">stream</Badge>}
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
              {conversationTurns(reqBody).length ? (
                <div className="max-h-96 space-y-3 overflow-auto rounded-md border border-border p-3">
                  {conversationTurns(reqBody).map((turn, i) => (
                    <div key={i}>
                      <p
                        className={`mb-0.5 text-[11px] font-medium uppercase tracking-wide ${
                          turn.role === 'assistant'
                            ? 'text-teal-600 dark:text-teal-400'
                            : 'text-muted-foreground'
                        }`}
                      >
                        {turn.role}
                      </p>
                      {turn.text ? (
                        <pre className="whitespace-pre-wrap break-all text-sm">
                          {decodeUnicodeEscapes(turn.text)}
                        </pre>
                      ) : null}
                      {turn.tools.map((name, j) => (
                        <p key={j} className="text-xs text-muted-foreground">
                          ⚙ {t('toolCall', { name })}
                        </p>
                      ))}
                      {turn.results.map((prev, j) => (
                        <p key={j} className="text-xs italic text-muted-foreground">
                          {t('toolResult')}: {decodeUnicodeEscapes(prev)}
                        </p>
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
            <JsonBlock value={resBody} />
          ) : resText ? (
            <div className="max-h-80 overflow-auto rounded-md border border-border p-3">
              <pre className="whitespace-pre-wrap break-all text-sm">{resText}</pre>
            </div>
          ) : sseText ? (
            // 델타 추출 실패 — SSE 원문 노출 (와이어의 \uXXXX 는 표시용 디코드)
            <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap break-all">
              {decodeUnicodeEscapes(sseText)}
            </pre>
          ) : (
            <JsonBlock value={resBody} />
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
