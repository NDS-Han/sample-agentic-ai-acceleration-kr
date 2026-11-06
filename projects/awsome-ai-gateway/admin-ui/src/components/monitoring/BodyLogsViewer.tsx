'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useEffect, useRef, useState, useTransition } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import { Copy, RefreshCw, Search } from 'lucide-react';

import { fetchBodyLog, type BodyLogResponse } from '@/lib/actions/body-logs';
import {
  fetchMonitoringEvents,
  type MonitoringEventsResponse,
  type MonitoringEvent,
  type MonitoringEventTypeFilter,
} from '@/lib/actions/monitoring';
import { AppDialog } from '@/components/common/AppDialog';
import { Badge } from '@/components/common/Badge';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { BodyRecordContent, statusTone } from './bodyLogRender';

// request_body 방언: anthropic/openai chat = messages[], responses API = input.
// response_body: 비스트림은 JSON, 스트림은 {sse_text, truncated?} (SSE 프레임 원문).

// 백엔드 _COLLECTING_WINDOW_S 와 같은 임계 — 이 안의 요청은 아직 Firehose
// 버퍼에 있을 수 있다. 배지로 "404 여도 아직 오는 중"을 표현한다.
const COLLECTING_WINDOW_MS = 180_000;

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

const TYPE_FILTERS: MonitoringEventTypeFilter[] = [
  'all',
  'error',
  'timeout',
  'slow',
  'abnormal',
  'success',
];

export function BodyLogsViewer({
  events: initialEvents,
  initialRequestId,
  onRequestLookup,
}: {
  events: MonitoringEventsResponse;
  /** ?request_id= 딥링크 — 진입 시 자동 조회(실제 열람이라 감사된다). */
  initialRequestId?: string | null;
  /** 조회 성공/실패 시 부모(URL)에 request_id 를 동기화. */
  onRequestLookup?: (_id: string | null) => void;
}) {
  const t = useTranslations('monitoring.bodyLogs');
  const tev = useTranslations('monitoring.events');
  const locale = useLocale();
  const tz = useReportingTz();
  const { toast } = useToast();
  const [query, setQuery] = useState(initialRequestId ?? '');
  const [loading, startTransition] = useTransition();
  const [result, setResult] = useState<BodyLogResponse | null>(null);
  /** 조회 실패는 토스트로 흘리지 않고 고정 카드에 남긴다 — 재시도가 다음 행동. */
  const [lookupError, setLookupError] = useState<{ id: string; message: string } | null>(null);
  const autoLookedUp = useRef<string | null>(null);

  const [events, setEvents] = useState<MonitoringEventsResponse>(initialEvents);
  const [typeFilter, setTypeFilter] = useState<MonitoringEventTypeFilter>('all');
  const [userFilter, setUserFilter] = useState('');
  // 페이지를 연 채 토글이 꺼지면 조회가 403 — 비활성 안내로 전환한다.
  const [toggledOff, setToggledOff] = useState(false);

  const lookup = (requestId: string) => {
    const id = requestId.trim();
    if (!id) return;
    setLookupError(null);
    startTransition(async () => {
      const res = await fetchBodyLog(id);
      if (res.success && res.data) {
        setResult(res.data);
        // ⚠️ onRequestLookup → router.replace(?request_id=id) → effect 가 같은
        // id 로 한 번 더 조회해 감사가 2행 생긴다 — URL 동기화 전에 처리됨 표시.
        autoLookedUp.current = id;
        onRequestLookup?.(id);
      } else if (res.status === 403) {
        setToggledOff(true);
      } else {
        setLookupError({ id, message: res.error ?? t('fetchFailed') });
      }
    });
  };

  // ?request_id= 딥링크 자동 조회 — 같은 id 중복 조회만 막는다.
  useEffect(() => {
    const id = initialRequestId?.trim();
    if (!id || autoLookedUp.current === id) return;
    autoLookedUp.current = id;
    setQuery(id);
    lookup(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialRequestId]);

  const refreshEvents = (filter: MonitoringEventTypeFilter = typeFilter) => {
    startTransition(async () => {
      try {
        setEvents(await fetchMonitoringEvents(200, filter, 0));
      } catch {
        toast({ type: 'error', message: tev('fetchFailed'), auto_dismiss_ms: 4000 });
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

  const uf = userFilter.trim().toLowerCase();
  const shown = events.events.filter(
    (ev) =>
      !uf ||
      (ev.user_email ?? '').toLowerCase().includes(uf) ||
      (ev.user_id ?? '').toLowerCase().includes(uf),
  );

  const isCollecting = (ev: MonitoringEvent) =>
    Date.now() - new Date(ev.timestamp).getTime() < COLLECTING_WINDOW_MS;

  if (toggledOff) {
    return (
      <div className="glass rounded-apple p-6">
        <p className="text-sm text-muted-foreground">{t('disabledNotice')}</p>
      </div>
    );
  }

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

        {loading && (
          <p className="mt-2 text-xs text-muted-foreground">{t('scanningHint')}</p>
        )}

        {lookupError && (
          <div className="mt-3 flex items-center justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/10 px-3 py-2">
            <p className="text-xs text-amber-700 dark:text-amber-400">
              <span className="font-mono">{lookupError.id.slice(0, 18)}…</span>{' '}
              {lookupError.message}
            </p>
            <button
              type="button"
              onClick={() => lookup(lookupError.id)}
              disabled={loading}
              className="shrink-0 text-xs font-medium text-primary hover:underline disabled:opacity-50"
            >
              {t('retry')}
            </button>
          </div>
        )}
      </div>

      {/* 최근 요청 — usage_logs 가 인덱스 역할 (S3 는 시간순 목록을 못 만든다) */}
      <div className="glass rounded-apple p-4">
        <div className="mb-3 flex flex-wrap items-center gap-2">
          <h2 className="text-base font-semibold">{t('recentTitle')}</h2>
          <div className="ml-auto flex items-center gap-2">
            <select
              value={typeFilter}
              onChange={(e) => {
                const v = e.target.value as MonitoringEventTypeFilter;
                setTypeFilter(v);
                refreshEvents(v); // 선택 즉시 적용 — stale 목록이 다른 필터로 보이지 않게
              }}
              className="rounded-md border border-border bg-background px-2 py-1.5 text-xs"
              aria-label={tev('typeLabel')}
            >
              {TYPE_FILTERS.map((f) => (
                <option key={f} value={f}>
                  {tev(`filters.${f}`)}
                </option>
              ))}
            </select>
            <input
              type="text"
              value={userFilter}
              onChange={(e) => setUserFilter(e.target.value)}
              placeholder={t('filterUserPlaceholder')}
              className="w-40 rounded-md border border-border bg-background px-2 py-1.5 text-xs"
            />
            <button
              type="button"
              onClick={() => refreshEvents()}
              disabled={loading}
              className="inline-flex items-center gap-1 text-xs text-primary hover:underline disabled:opacity-50"
            >
              <RefreshCw size={12} aria-hidden="true" />
              {t('refresh')}
            </button>
          </div>
        </div>
        {shown.length === 0 ? (
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
              {shown.map((ev: MonitoringEvent) => (
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
                  <Td className="max-w-40 truncate text-sm" title={ev.user_email ?? ev.user_id ?? ''}>
                    {ev.user_email ?? ev.user_id ?? '—'}
                  </Td>
                  <Td className="text-sm">{ev.model_alias}</Td>
                  <Td>
                    <Badge tone={statusTone(ev.event_type === 'SUCCESS' ? 'success' : ev.event_type === 'SLOW_REQUEST' ? 'partial' : 'error')}>
                      {ev.event_type}
                    </Badge>
                  </Td>
                  <Td>
                    <div className="flex items-center gap-2">
                      <button
                        type="button"
                        onClick={() => lookup(ev.request_id)}
                        disabled={loading}
                        className="text-xs font-medium text-primary hover:underline disabled:opacity-50"
                      >
                        {t('viewBody')}
                      </button>
                      {isCollecting(ev) && (
                        <Badge tone="amber">{t('collecting')}</Badge>
                      )}
                    </div>
                  </Td>
                </Tr>
              ))}
            </TBody>
          </Table>
        )}
      </div>

      {result && (
        <BodyLogDialog
          data={result}
          onClose={() => {
            setResult(null);
            // 닫아도 ?request_id= 가 남으면 새로고침 때 다시 조회돼 감사가
            // 한 번 더 생긴다 — 닫을 때 URL 도 정리한다. autoLookedUp 도
            // 리셋해 같은 id 딥링크 재진입이 동작하게 한다.
            autoLookedUp.current = null;
            onRequestLookup?.(null);
          }}
        />
      )}
    </div>
  );
}
