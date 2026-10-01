'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useRef, useState, useTransition } from 'react';
import { useLocale, useTranslations } from 'next-intl';
import {
  fetchMonitoringEvents,
  type MonitoringEventsResponse,
  type MonitoringEventTypeFilter,
} from '@/lib/actions/monitoring';
import type { BadgeTone } from '@/components/common/Badge';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtDateTime } from '@/lib/utils/format';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

function eventTone(type: string): BadgeTone {
  switch (type) {
    case 'ERROR':
      return 'pink';
    case 'TIMEOUT':
    case 'SLOW_REQUEST':
      return 'amber';
    case 'SUCCESS':
      return 'teal';
    default:
      return 'neutral';
  }
}

export function EventLog({ data: initialData }: { data: MonitoringEventsResponse }) {
  const t = useTranslations('monitoring');
  const locale = useLocale();
  const tz = useReportingTz();
  const { toast } = useToast();
  const [filter, setFilter] = useState<MonitoringEventTypeFilter>('all');
  const [data, setData] = useState<MonitoringEventsResponse>(initialData);
  const [isPending, startTransition] = useTransition();

  const EVENT_LABELS: Record<string, string> = {
    ERROR: t('events.types.ERROR'),
    TIMEOUT: t('events.types.TIMEOUT'),
    SLOW_REQUEST: t('events.types.SLOW_REQUEST'),
    SUCCESS: t('events.types.SUCCESS'),
  };

  const FILTER_OPTIONS: { value: MonitoringEventTypeFilter; label: string }[] = [
    { value: 'all', label: t('events.filters.all') },
    { value: 'success', label: t('events.filters.success') },
    { value: 'error', label: t('events.filters.error') },
    { value: 'timeout', label: t('events.filters.timeout') },
    { value: 'slow', label: t('events.filters.slow') },
    { value: 'abnormal', label: t('events.filters.abnormal') },
  ];

  const handleFilterChange = (next: MonitoringEventTypeFilter) => {
    setFilter(next);
    startTransition(async () => {
      // 필터 변경은 사용자 액션 — 실패를 조용히 삼키면 선택만 바뀌고 목록은
      // 이전 필터의 데이터로 보인다. 폴링과 달리 명시 알림이 필요하다.
      const fresh = await fetchMonitoringEvents(50, next).catch(() => null);
      if (fresh === null) {
        toast({
          type: 'error',
          message: t('events.fetchFailed'),
          auto_dismiss_ms: 4000,
        });
        return;
      }
      setData(fresh);
    });
  };

  // 모니터링 페이지 — 30초 자동 새로고침(탭 비활성 시 폴링 중지). 수동 리로드에
  // 의존하면 "실시간" 로그가 아니다. 필터 상태는 ref 로 추적해 클로저 고착 방지.
  const filterRef = useRef(filter);
  filterRef.current = filter;
  useEffect(() => {
    const id = setInterval(() => {
      if (document.visibilityState !== 'visible') return;
      void fetchMonitoringEvents(50, filterRef.current)
        .then(setData)
        .catch(() => undefined);
    }, 30_000);
    return () => clearInterval(id);
  }, []);

  const copyUserId = (id: string) => {
    navigator.clipboard?.writeText(id).catch(() => undefined);
  };

  return (
    <div className="glass rounded-apple overflow-hidden">
      <div className="px-4 py-3 border-b border-border flex items-center justify-between gap-3 flex-wrap">
        <h3 className="text-sm font-semibold">{t('events.title')}</h3>
        <div className="flex items-center gap-2">
          <label htmlFor="event-type-filter" className="text-xs text-muted-foreground">
            {t('events.typeLabel')}
          </label>
          <select
            id="event-type-filter"
            value={filter}
            onChange={(e) => handleFilterChange(e.target.value as MonitoringEventTypeFilter)}
            disabled={isPending}
            className="text-xs border border-border rounded px-2 py-1 bg-background"
          >
            {FILTER_OPTIONS.map((opt) => (
              <option key={opt.value} value={opt.value}>
                {opt.label}
              </option>
            ))}
          </select>
        </div>
      </div>

      {/* 필터 재조회 중엔 stale 행이 그대로 보인다 — 흐리게 표시해 "예전 결과"임을 전달 */}
      <div aria-busy={isPending} className={isPending ? 'opacity-50 transition-opacity' : 'transition-opacity'}>
      {data.events.length === 0 ? (
        <div className="p-6">
          <p className="text-sm text-muted-foreground">{t('events.empty')}</p>
        </div>
      ) : (
        <Table density="compact">
          <THead>
            <Tr>
              <Th>{t('events.colTime')}</Th>
              <Th>{t('events.colType')}</Th>
              <Th>{t('events.colModel')}</Th>
              <Th>{t('events.colUser')}</Th>
              <Th>{t('events.colDetail')}</Th>
            </Tr>
          </THead>
          <TBody>
            {data.events.map((ev, i) => (
              <Tr key={`${ev.timestamp}-${i}`}>
                <Td className="text-muted-foreground whitespace-nowrap num">
                  {fmtDateTime(ev.timestamp, locale, tz)}
                </Td>
                <Td>
                  <span className={`badge badge-${eventTone(ev.event_type)}`}>
                    {EVENT_LABELS[ev.event_type] ?? ev.event_type}
                  </span>
                </Td>
                <Td emphasis>
                  {ev.downgraded_from ? (
                    <span className="inline-flex items-center gap-1">
                      <span className="text-muted-foreground line-through text-xs font-mono mono-id">
                        {ev.downgraded_from}
                      </span>
                      <span className="text-muted-foreground">→</span>
                      <span className="font-mono mono-id text-xs">{ev.model_alias}</span>
                    </span>
                  ) : (
                    <span className="font-mono mono-id text-xs">{ev.model_alias}</span>
                  )}
                </Td>
                <Td className="text-muted-foreground font-mono mono-id text-xs">
                  <button
                    type="button"
                    title={ev.user_id}
                    onClick={() => copyUserId(ev.user_id)}
                    className="hover:underline underline-offset-2 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring rounded-sm"
                    aria-label={`${ev.user_id} — ${t('events.copyUserId')}`}
                  >
                    {ev.user_id.slice(0, 8)}…
                  </button>
                </Td>
                <Td className="text-muted-foreground">{ev.detail}</Td>
              </Tr>
            ))}
          </TBody>
        </Table>
      )}
      </div>
    </div>
  );
}
