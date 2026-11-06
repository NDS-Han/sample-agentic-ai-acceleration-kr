'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useCallback } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';

import type { MonitoringEventsResponse } from '@/lib/actions/monitoring';
import { BodyLogsViewer } from './BodyLogsViewer';
import { BodyLogSessions } from './BodyLogSessions';

type Mode = 'lookup' | 'sessions';

export function BodyLogsTabs({ events }: { events: MonitoringEventsResponse }) {
  const t = useTranslations('monitoring.bodyLogs');
  const pathname = usePathname();
  const sp = useSearchParams();

  // 탭/조회 대상은 URL 이 진실 소스 — 새로고침·공유 링크가 같은 화면을 연다.
  // 자동 오픈도 실제 열람이라 감사 행이 생기는 게 맞다.
  const mode: Mode = sp.get('tab') === 'lookup' ? 'lookup' : 'sessions';
  const requestId = sp.get('request_id');
  const sessionKey = sp.get('session');

  const setParams = useCallback(
    (next: Record<string, string | null>) => {
      const params = new URLSearchParams(sp.toString());
      for (const [k, v] of Object.entries(next)) {
        if (v === null) params.delete(k);
        else params.set(k, v);
      }
      // router.replace 는 서버 컴포넌트 재실행(토글 조회+이벤트 200건 재조회)을
      // 유발한다 — 세션 열고 닫을 때마다 재렌더되는 건 낭비라 네이티브 history
      // 로만 바꾼다. Next 15는 replaceState를 useSearchParams와 연동한다.
      window.history.replaceState(null, '', `${pathname}?${params.toString()}`);
    },
    [pathname, sp],
  );

  const setMode = (m: Mode) =>
    setParams({
      tab: m,
      // 탭을 바꾸면 다른 탭의 딥링크 대상은 버린다 — 양쪽 state 를 섞지 않는다.
      session: null,
      request_id: null,
    });

  const onSessionChange = useCallback(
    (key: string | null) => setParams({ session: key }),
    [setParams],
  );
  const onRequestLookup = useCallback(
    (id: string | null) => setParams({ request_id: id }),
    [setParams],
  );

  return (
    <div className="space-y-4">
      <div className="flex gap-1 rounded-lg bg-muted p-1 w-fit">
        {(['sessions', 'lookup'] as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            onClick={() => setMode(m)}
            className={`rounded-md px-4 py-1.5 text-sm font-medium transition-colors ${
              mode === m
                ? 'bg-background text-foreground shadow-sm'
                : 'text-muted-foreground hover:text-foreground'
            }`}
          >
            {m === 'sessions' ? t('tabSessions') : t('tabLookup')}
          </button>
        ))}
      </div>
      {mode === 'sessions' ? (
        <BodyLogSessions
          initialSessionKey={sessionKey}
          onSessionChange={onSessionChange}
        />
      ) : (
        <BodyLogsViewer
          events={events}
          initialRequestId={requestId}
          onRequestLookup={onRequestLookup}
        />
      )}
    </div>
  );
}
