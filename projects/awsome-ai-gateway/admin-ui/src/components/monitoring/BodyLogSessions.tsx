'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useState, useTransition } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';

import {
  fetchBodyLogSessions,
  fetchBodyLogBatch,
  type BodyLogBatchResponse,
  type BodyLogRecord,
  type BodyLogResponse,
  type BodyLogSession,
} from '@/lib/actions/body-logs';
import { Badge } from '@/components/common/Badge';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { BodyRecordContent, statusTone } from './bodyLogRender';

// 배치 응답 아이템 → 단건 뷰가 기대하는 BodyLogResponse 형태로 맞춘다.
function toBodyLogResponse(
  requestId: string,
  item: {
    source: 'firehose' | 'fallback' | null;
    key: string | null;
    duplicates: number;
    record: BodyLogRecord | null;
    model_alias?: string;
    user_email?: string | null;
    requested_at?: string;
  },
  oversized: boolean,
  incomplete: boolean,
): BodyLogResponse {
  return {
    found: true,
    request_id: requestId,
    source: item.source,
    key: item.key,
    duplicates: item.duplicates,
    oversized,
    incomplete,
    model_alias: item.model_alias ?? '',
    user_email: item.user_email,
    requested_at: item.requested_at ?? '',
    record: item.record,
  };
}

export function BodyLogSessions() {
  const t = useTranslations('monitoring.bodyLogs');
  const locale = useLocale();
  const tz = useReportingTz();
  const { toast } = useToast();

  const [sessions, setSessions] = useState<BodyLogSession[] | null>(null);
  const [truncated, setTruncated] = useState(false);
  const [loading, startTransition] = useTransition();

  const [selected, setSelected] = useState<BodyLogSession | null>(null);
  const [batch, setBatch] = useState<BodyLogBatchResponse | null>(null);
  const [traceLoading, startTraceTransition] = useTransition();
  const [openCalls, setOpenCalls] = useState<Set<string>>(new Set());

  const load = () => {
    startTransition(async () => {
      const res = await fetchBodyLogSessions(1);
      if (res.success && res.data) {
        setSessions(res.data.sessions);
        setTruncated(res.data.truncated);
      } else {
        toast({ type: 'error', message: res.error ?? t('fetchFailed'), auto_dismiss_ms: 6000 });
      }
    });
  };

  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const openSession = (s: BodyLogSession) => {
    setSelected(s);
    setBatch(null);
    setOpenCalls(new Set());
    startTraceTransition(async () => {
      const res = await fetchBodyLogBatch(s.calls.map((c) => c.request_id));
      if (res.success && res.data) {
        setBatch(res.data);
      } else {
        toast({ type: 'error', message: res.error ?? t('fetchFailed'), auto_dismiss_ms: 6000 });
        setSelected(null);
      }
    });
  };

  const toggleCall = (rid: string) => {
    setOpenCalls((prev) => {
      const next = new Set(prev);
      if (next.has(rid)) next.delete(rid);
      else next.add(rid);
      return next;
    });
  };

  // ── 세션 trace 뷰 ────────────────────────────────────────────────────────────
  if (selected) {
    const missing = new Set(batch?.missing ?? []);
    const oversized = new Set(batch?.oversized ?? []);
    return (
      <div className="space-y-4">
        <button
          type="button"
          onClick={() => setSelected(null)}
          className="text-sm text-primary hover:underline"
        >
          {t('backToSessions')}
        </button>

        {/* 세션 헤더 */}
        <div className="glass rounded-apple p-4">
          <div className="flex flex-wrap items-center gap-2 text-sm">
            <span className="font-medium">{selected.user_email ?? selected.user_id ?? '-'}</span>
            {selected.client && <Badge tone="neutral">{selected.client}</Badge>}
            <span className="text-muted-foreground">
              {fmtDateTime(selected.started_at, locale, tz)} → {fmtDateTime(selected.ended_at, locale, tz)}
            </span>
            <Badge tone="neutral">{t('callsCount', { count: selected.call_count })}</Badge>
            {selected.error_count > 0 && (
              <Badge tone="pink">{t('errorsCount', { count: selected.error_count })}</Badge>
            )}
            {batch?.incomplete && <Badge tone="amber">{t('scanIncomplete')}</Badge>}
          </div>
        </div>

        {/* 호출 타임라인 */}
        {traceLoading ? (
          <p className="text-sm text-muted-foreground">{t('loadingTrace')}</p>
        ) : (
          <div className="space-y-2">
            {selected.calls.map((call, i) => {
              const item = batch?.records[call.request_id];
              const isMissing = missing.has(call.request_id);
              const isOpen = openCalls.has(call.request_id);
              return (
                <div key={call.request_id} className="glass rounded-apple">
                  <button
                    type="button"
                    onClick={() => item && toggleCall(call.request_id)}
                    disabled={!item}
                    className="flex w-full flex-wrap items-center gap-2 px-4 py-3 text-left text-sm disabled:cursor-default"
                  >
                    {item ? (
                      isOpen ? (
                        <ChevronDown size={14} aria-hidden="true" />
                      ) : (
                        <ChevronRight size={14} aria-hidden="true" />
                      )
                    ) : (
                      <ChevronRight size={14} className="text-muted-foreground" aria-hidden="true" />
                    )}
                    <span className="w-8 text-xs text-muted-foreground">#{i + 1}</span>
                    <span className="whitespace-nowrap">
                      {fmtDateTime(call.requested_at, locale, tz)}
                    </span>
                    <span>{call.model_alias ?? '-'}</span>
                    <Badge tone={statusTone(call.status === 'SUCCESS' ? 'success' : 'error')}>
                      {call.status ?? '-'}
                    </Badge>
                    {call.latency_ms != null && (
                      <span className="text-xs text-muted-foreground">
                        {(call.latency_ms / 1000).toFixed(1)}s
                      </span>
                    )}
                    {isMissing && <Badge tone="neutral">{t('notCollected')}</Badge>}
                    {oversized.has(call.request_id) && (
                      <Badge tone="amber">{t('oversized')}</Badge>
                    )}
                    {item?.duplicates ? (
                      <Badge tone="neutral">
                        {t('duplicates', { count: item.duplicates })}
                      </Badge>
                    ) : null}
                  </button>
                  {isOpen && item && (
                    <div className="border-t border-border px-4 py-3">
                      <BodyRecordContent
                        data={toBodyLogResponse(
                          call.request_id,
                          item,
                          oversized.has(call.request_id),
                          batch?.incomplete ?? false,
                        )}
                      />
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    );
  }

  // ── 세션 목록 ────────────────────────────────────────────────────────────────
  return (
    <div className="glass rounded-apple p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-base font-semibold">{t('sessionsTitle')}</h2>
        <button
          type="button"
          onClick={load}
          disabled={loading}
          className="inline-flex items-center gap-1 text-xs text-primary hover:underline disabled:opacity-50"
        >
          <RefreshCw size={12} aria-hidden="true" />
          {t('refresh')}
        </button>
      </div>
      {sessions === null ? (
        <p className="text-sm text-muted-foreground">{t('loadingTrace')}</p>
      ) : sessions.length === 0 ? (
        <p className="text-sm text-muted-foreground">{t('sessionsEmpty')}</p>
      ) : (
        <>
          {truncated && (
            <p className="mb-2 text-xs text-amber-600 dark:text-amber-400">
              {t('sessionsTruncated')}
            </p>
          )}
          <Table>
            <THead>
              <Tr>
                <Th>{t('colEnded')}</Th>
                <Th>{t('colUser')}</Th>
                <Th>{t('colClient')}</Th>
                <Th>{t('colCalls')}</Th>
                <Th>{t('colModels')}</Th>
                <Th>{t('colErrors')}</Th>
              </Tr>
            </THead>
            <TBody>
              {sessions.map((s) => (
                <Tr
                  key={s.key}
                  className="cursor-pointer hover:bg-muted/50"
                  onClick={() => openSession(s)}
                >
                  <Td className="whitespace-nowrap">{fmtDateTime(s.ended_at, locale, tz)}</Td>
                  <Td className="max-w-40 truncate text-sm" title={s.user_email ?? s.user_id ?? ''}>
                    {s.user_email ?? s.user_id ?? '-'}
                  </Td>
                  <Td className="text-sm">{s.client ?? '-'}</Td>
                  <Td className="text-sm">{s.call_count}</Td>
                  <Td className="max-w-48 truncate text-sm" title={s.models.join(', ')}>
                    {s.models.join(', ')}
                  </Td>
                  <Td>
                    {s.error_count > 0 ? (
                      <Badge tone="pink">{s.error_count}</Badge>
                    ) : (
                      <span className="text-xs text-muted-foreground">0</span>
                    )}
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        </>
      )}
    </div>
  );
}
