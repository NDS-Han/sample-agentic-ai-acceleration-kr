'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useLocale, useTranslations } from 'next-intl';
import { AlertTriangle, AlertCircle } from 'lucide-react';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { fmtTime } from '@/lib/utils/format';
import type { MonitoringModelsResponse } from '@/lib/actions/monitoring';
import { Badge } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';

// 에러율 강조 — 테마 토큰(다크/라이트 자동). 임계: ≥10% 위험, ≥5% 경고.
function errorColor(pct: number) {
  if (pct >= 10) return 'text-destructive font-semibold';
  if (pct >= 5) return 'text-amber-600 dark:text-amber-400';
  return '';
}

// 색상만으로 심각도를 구분하면 색각 이상 사용자에게 전달되지 않는다 — 아이콘 병기.
function SeverityIcon({ pct }: { pct: number }) {
  if (pct >= 10) return <AlertTriangle size={13} aria-hidden="true" />;
  if (pct >= 5) return <AlertCircle size={13} aria-hidden="true" />;
  return null;
}

export function ModelHealthTable({ data }: { data: MonitoringModelsResponse }) {
  const t = useTranslations('monitoring');
  const locale = useLocale();
  const tz = useReportingTz();

  if (data.models.length === 0) {
    return (
      <div className="glass rounded-apple p-6">
        <p className="text-sm text-muted-foreground">{t('models.empty')}</p>
      </div>
    );
  }

  return (
    <div className="glass rounded-apple overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold tracking-tight">{t('models.title')}</h3>
      </div>
      <Table>
        <THead>
          <Tr>
            <Th>{t('table.model')}</Th>
            <Th>{t('table.status')}</Th>
            <Th numeric>{t('table.requests')}</Th>
            <Th numeric>{t('table.avgLatency')}</Th>
            <Th numeric>{t('table.errorRate')}</Th>
            <Th numeric>{t('table.lastRequest')}</Th>
          </Tr>
        </THead>
        <TBody>
          {data.models.map((m) => (
            <Tr key={m.alias}>
              <Td emphasis className="font-mono mono-id text-xs">{m.alias}</Td>
              <Td>
                <Badge tone={m.status === 'ACTIVE' ? 'teal' : 'neutral'}>{m.status}</Badge>
              </Td>
              <Td numeric>{m.last_1h_requests.toLocaleString()}</Td>
              <Td numeric>{m.avg_latency_ms}ms</Td>
              <Td numeric className={errorColor(m.error_rate_pct)}>
                <span className="inline-flex items-center justify-end gap-1">
                  <SeverityIcon pct={m.error_rate_pct} />
                  {m.error_rate_pct}%
                  {m.error_rate_pct >= 5 && (
                    <span className="sr-only">
                      {m.error_rate_pct >= 10
                        ? t('table.severityCritical')
                        : t('table.severityWarning')}
                    </span>
                  )}
                </span>
              </Td>
              <Td numeric className="text-muted-foreground">
                {m.last_request_at
                  ? fmtTime(m.last_request_at, locale, tz)
                  : '-'}
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}