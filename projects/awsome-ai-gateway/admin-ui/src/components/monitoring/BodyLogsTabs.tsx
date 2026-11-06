'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useState } from 'react';
import { useTranslations } from 'next-intl';

import type { MonitoringEventsResponse } from '@/lib/actions/monitoring';
import { BodyLogsViewer } from './BodyLogsViewer';
import { BodyLogSessions } from './BodyLogSessions';

type Mode = 'lookup' | 'sessions';

export function BodyLogsTabs({ events }: { events: MonitoringEventsResponse }) {
  const t = useTranslations('monitoring.bodyLogs');
  const [mode, setMode] = useState<Mode>('sessions');

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
      {mode === 'sessions' ? <BodyLogSessions /> : <BodyLogsViewer events={events} />}
    </div>
  );
}
