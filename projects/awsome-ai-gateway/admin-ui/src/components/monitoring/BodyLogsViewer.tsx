'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState, useTransition } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Copy, Search } from 'lucide-react';

import { fetchBodyLog, type BodyLogResponse } from '@/lib/actions/body-logs';
import type { MonitoringEventsResponse, MonitoringEvent } from '@/lib/actions/monitoring';
import { AppDialog } from '@/components/common/AppDialog';
import { Badge } from '@/components/common/Badge';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { BodyRecordContent, statusTone } from './bodyLogRender';

// request_body 방언: anthropic/openai chat = messages[], responses API = input.
// response_body: 비스트림은 JSON, 스트림은 {sse_text, truncated?} (SSE 프레임 원문).

function BodyLogDialog({
  data,
  onClose,
}: {
  data: BodyLogResponse;
  onClose: () => void;
}) {
  const t = useTranslations('monitoring.bodyLogs');
  return (
    <AppDialog
      isOpen
      onClose={onClose}
      title={t('viewerTitle')}
      wide
      contentClassName="max-w-5xl"
    >
      <BodyRecordContent data={data} />
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
                <Th>{t('colUser')}</Th>
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
                  <Td className="max-w-40 truncate text-sm" title={ev.user_email ?? ev.user_id}>
                    {ev.user_email ?? ev.user_id}
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
