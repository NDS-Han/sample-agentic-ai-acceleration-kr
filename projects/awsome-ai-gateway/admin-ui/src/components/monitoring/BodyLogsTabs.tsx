'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useCallback } from 'react';
import { usePathname, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';

import { BodyLogsViewer } from './BodyLogsViewer';
import { BodyLogSessions, type SessionFilters } from './BodyLogSessions';

type Mode = 'lookup' | 'sessions';

export function BodyLogsTabs() {
  const t = useTranslations('monitoring.bodyLogs');
  const pathname = usePathname();
  const sp = useSearchParams();

  // 탭/조회 대상은 URL 이 진실 소스 — 새로고침·공유 링크가 같은 화면을 연다.
  // 자동 오픈도 실제 열람이라 감사 행이 생기는 게 맞다.
  // request_id 만 있는 링크는 lookup 으로 자동 유도 — tab 파라미터 없이
  // 공유해도 조용히 세션 탭에 떨어지지 않게 한다.
  const requestId = sp.get('request_id');
  const sessionKey = sp.get('session');
  const mode: Mode =
    sp.get('tab') === 'lookup' || requestId ? 'lookup' : 'sessions';

  const setParams = useCallback(
    (next: Record<string, string | null>, opts?: { push?: boolean }) => {
      const params = new URLSearchParams(sp.toString());
      for (const [k, v] of Object.entries(next)) {
        if (v === null) params.delete(k);
        else params.set(k, v);
      }
      // router.replace 는 서버 컴포넌트 재실행(토글 조회 재조회)을 유발한다 —
      // 네이티브 history 로만 바꾼다. Next 15는 push/replaceState 를
      // useSearchParams 와 연동한다.
      const url = `${pathname}?${params.toString()}`;
      if (opts?.push) window.history.pushState(null, '', url);
      else window.history.replaceState(null, '', url);
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

  // 세션 열기만 push — 뒤로가기가 "세션 닫기"로 읽히게 한다. 닫기/필터는
  // replace 라 히스토리에 조작마다 항목이 쌓이지 않는다.
  const onSessionChange = useCallback(
    (key: string | null) => setParams({ session: key }, { push: Boolean(key) }),
    [setParams],
  );
  const onRequestLookup = useCallback(
    (id: string | null) => setParams({ request_id: id }),
    [setParams],
  );

  // 적용된 필터도 URL 에 둔다 — "어제 A씨 실패 세션" 목록을 링크로 공유할 수
  // 있게. 입력 중(미적용) 값은 URL 에 쓰지 않는다.
  const onFiltersApplied = useCallback(
    (f: SessionFilters) =>
      setParams({
        days: f.days === 1 ? null : String(f.days),
        user: f.user || null,
        client: f.client || null,
        err: f.hasError ? '1' : null,
      }),
    [setParams],
  );

  const initialFilters: SessionFilters = {
    days: [1, 7, 30, 90].includes(Number(sp.get('days'))) ? Number(sp.get('days')) : 1,
    user: sp.get('user') ?? '',
    client: sp.get('client') ?? '',
    hasError: sp.get('err') === '1',
  };

  return (
    <div className="space-y-4">
      <div className="flex gap-1 rounded-lg bg-muted p-1 w-fit" role="tablist">
        {(['sessions', 'lookup'] as Mode[]).map((m) => (
          <button
            key={m}
            type="button"
            role="tab"
            aria-selected={mode === m}
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
          initialFilters={initialFilters}
          onSessionChange={onSessionChange}
          onFiltersApplied={onFiltersApplied}
        />
      ) : (
        <BodyLogsViewer
          initialRequestId={requestId}
          onRequestLookup={onRequestLookup}
        />
      )}
    </div>
  );
}
