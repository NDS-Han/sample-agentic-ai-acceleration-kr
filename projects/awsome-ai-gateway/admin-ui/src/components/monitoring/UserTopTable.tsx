'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useTranslations } from 'next-intl';
import Link from 'next/link';
import { usePointInTime } from '@/components/common/usePointInTime';
import type { MonitoringUsersResponse } from '@/lib/actions/monitoring';
import { Table, THead, TBody, Tr, Th, Td } from '@/components/common/Table';
import { EmailText } from '@/components/common/UnverifiedEmailBadge';
import { Usd } from '@/components/common/Usd';

// 에러율 강조 — 테마 토큰(다크/라이트 자동). 임계: ≥10% 위험, ≥5% 경고.
function errorColor(pct: number) {
  if (pct >= 10) return 'text-destructive font-semibold';
  if (pct >= 5) return 'text-amber-600 dark:text-amber-400';
  return '';
}

export function UserTopTable({ data }: { data: MonitoringUsersResponse }) {
  const t = useTranslations('monitoring');
  const pit = usePointInTime();

  if (data.users.length === 0) {
    return (
      <div className="glass rounded-apple p-6 space-y-1">
        <p className="text-sm text-muted-foreground">{t('users.empty')}</p>
        <Link href="/analytics" className="text-xs text-primary hover:underline">
          {t('emptyAnalyticsLink')}
        </Link>
      </div>
    );
  }

  return (
    <div className="glass rounded-apple overflow-hidden">
      <div className="px-4 py-3 border-b border-border">
        <h3 className="text-sm font-semibold tracking-tight">{t('users.title', { count: data.users.length })}</h3>
      </div>
      <Table>
        <THead>
          <Tr>
            <Th>{t('table.user')}</Th>
            <Th numeric>{t('table.requests')}</Th>
            <Th numeric>{t('table.tokens')}</Th>
            <Th numeric>{t('table.costUsd')}</Th>
            <Th numeric>{t('table.errorRate')}</Th>
            <Th numeric title={pit.title}>
              {pit.header(t('table.lastRequest'))}
            </Th>
          </Tr>
        </THead>
        <TBody>
          {data.users.map((u) => (
            <Tr key={u.user_id}>
              <Td>
                <div className="font-medium">{u.display_name}</div>
                <div className="text-xs text-muted-foreground">
                  <EmailText email={u.email} />
                </div>
              </Td>
              <Td numeric>{u.requests.toLocaleString()}</Td>
              <Td numeric>{u.tokens.toLocaleString()}</Td>
              <Td numeric emphasis><Usd value={u.cost_usd} /></Td>
              <Td numeric className={errorColor(u.error_rate_pct)}>
                {u.error_rate_pct}%
              </Td>
              <Td numeric className="text-muted-foreground" title={pit.cellTitle(u.last_request_at)}>
                {u.last_request_at
                  ? pit.time(u.last_request_at)
                  : '-'}
              </Td>
            </Tr>
          ))}
        </TBody>
      </Table>
    </div>
  );
}