'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState, useTransition } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Copy, Search } from 'lucide-react';

import { fetchBodyLog, type BodyLogResponse } from '@/lib/actions/body-logs';
import type { MonitoringEventsResponse, MonitoringEvent } from '@/lib/actions/monitoring';
import { AppDialog } from '@/components/common/AppDialog';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

// ── 본문 텍스트 추출 ──────────────────────────────────────────────────────────
// request_body 방언: anthropic/openai chat = messages[], responses API = input.
// response_body: 비스트림은 JSON, 스트림은 {sse_text, truncated?} (SSE 프레임 원문).

interface ChatMessage {
  role?: string;
  content?: unknown;
}

function textFromContent(content: unknown): string {
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

function lastUserText(body: Record<string, unknown>): string {
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

/** SSE 프레임 원문에서 assistant 텍스트 델타를 추출 — anthropic / openai / responses 방언. */
function extractSseText(sse: string): string {
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

function nonstreamResponseText(body: Record<string, unknown>): string {
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

function statusTone(status: string): BadgeTone {
  if (status === 'error') return 'pink';
  if (status === 'partial') return 'amber';
  return 'teal';
}

// ── 뷰어 ──────────────────────────────────────────────────────────────────────

function JsonBlock({ value }: { value: unknown }) {
  return (
    <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-all">
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

function BodyLogDialog({
  data,
  onClose,
}: {
  data: BodyLogResponse;
  onClose: () => void;
}) {
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

  const userText = lastUserText(reqBody);
  const resText = sseText ? extractSseText(sseText) : nonstreamResponseText(resBody);
  const unparseable = reqBody._unparseable === true;
  const rawWrapped = '_raw' in reqBody;

  return (
    <AppDialog
      isOpen
      onClose={onClose}
      title={t('viewerTitle')}
      wide
      contentClassName="max-w-5xl"
    >
      {/* 메타 */}
      <div className="mb-4 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <Badge tone={statusTone(rec?.status ?? '')}>{rec?.status ?? '-'}</Badge>
        <span>{rec?.model_alias ?? data.model_alias}</span>
        <span>·</span>
        <span>{data.requested_at ? fmtDateTime(data.requested_at, locale, tz) : '-'}</span>
        <span>·</span>
        <span className="font-mono">{data.request_id}</span>
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
              {userText ? (
                <div className="max-h-64 overflow-auto rounded-md border border-border p-3">
                  <p className="mb-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    {t('lastUserMessage')}
                  </p>
                  <pre className="whitespace-pre-wrap break-all text-sm">{userText}</pre>
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
            // 델타 추출 실패 — SSE 원문 노출
            <pre className="max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs whitespace-pre-wrap break-all">
              {sseText}
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
    </AppDialog>
  );
}

// ── 페이지 본체 ───────────────────────────────────────────────────────────────

export function BodyLogsViewer({ events }: { events: MonitoringEventsResponse }) {
  const t = useTranslations('monitoring.bodyLogs');
  const locale = useLocale();
  const tz = useReportingTz();
  const { toast } = useToast();
  const [query, setQuery] = useState('');
  const [loading, startTransition] = useTransition();
  const [result, setResult] = useState<BodyLogResponse | null>(null);

  const lookup = (requestId: string) => {
    const id = requestId.trim();
    if (!id) return;
    startTransition(async () => {
      const res = await fetchBodyLog(id);
      if (res.success && res.data) {
        setResult(res.data);
      } else {
        toast({
          type: res.status === 404 ? 'info' : 'error',
          message: res.error ?? t('fetchFailed'),
          auto_dismiss_ms: 6000,
        });
      }
    });
  };

  const copyId = async (id: string) => {
    try {
      await navigator.clipboard.writeText(id);
      toast({ type: 'success', message: t('copied'), auto_dismiss_ms: 1500 });
    } catch {
      /* clipboard 거부 시 무시 */
    }
  };

  return (
    <div className="space-y-6">
      {/* request_id 직접 검색 — 사용자가 X-Request-Id 를 들고 오는 주 진입점 */}
      <div className="glass rounded-apple p-4">
        <label className="mb-2 block text-sm font-medium">{t('searchLabel')}</label>
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            lookup(query);
          }}
        >
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('searchPlaceholder')}
            className="flex-1 rounded-md border border-border bg-background px-3 py-2 font-mono text-sm focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
          />
          <button
            type="submit"
            disabled={loading || !query.trim()}
            className="inline-flex items-center gap-1.5 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
          >
            <Search size={14} aria-hidden="true" />
            {t('searchButton')}
          </button>
        </form>
        <p className="mt-2 text-xs text-muted-foreground">{t('searchHint')}</p>
      </div>

      {/* 최근 요청 — usage_logs 가 인덱스 역할 (S3 는 시간순 목록을 못 만든다) */}
      <div className="glass rounded-apple p-4">
        <h2 className="mb-3 text-base font-semibold">{t('recentTitle')}</h2>
        {events.events.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t('recentEmpty')}</p>
        ) : (
          <Table>
            <THead>
              <Tr>
                <Th>{t('colTime')}</Th>
                <Th>{t('colRequestId')}</Th>
                <Th>{t('colModel')}</Th>
                <Th>{t('colType')}</Th>
                <Th>{t('colAction')}</Th>
              </Tr>
            </THead>
            <TBody>
              {events.events.map((ev: MonitoringEvent) => (
                <Tr key={ev.request_id}>
                  <Td className="whitespace-nowrap">{fmtDateTime(ev.timestamp, locale, tz)}</Td>
                  <Td>
                    <button
                      type="button"
                      onClick={() => copyId(ev.request_id)}
                      className="inline-flex items-center gap-1 font-mono text-xs text-primary hover:underline"
                      title={ev.request_id}
                    >
                      {ev.request_id.slice(0, 8)}…
                      <Copy size={12} aria-hidden="true" />
                    </button>
                  </Td>
                  <Td className="text-sm">{ev.model_alias}</Td>
                  <Td>
                    <Badge tone={statusTone(ev.event_type === 'SUCCESS' ? 'success' : ev.event_type === 'SLOW_REQUEST' ? 'partial' : 'error')}>
                      {ev.event_type}
                    </Badge>
                  </Td>
                  <Td>
                    <button
                      type="button"
                      onClick={() => lookup(ev.request_id)}
                      disabled={loading}
                      className="text-xs font-medium text-primary hover:underline disabled:opacity-50"
                    >
                      {t('viewBody')}
                    </button>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>

      {result && <BodyLogDialog data={result} onClose={() => setResult(null)} />}
    </div>
  );
}
