'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Fragment, useState, useTransition } from 'react';
import Link from 'next/link';
import { useLocale, useTranslations } from 'next-intl';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { listAuditLogsAction } from '@/lib/actions/audit';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { Badge } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { fmtDateTime } from '@/lib/utils/format';
import { ChangesPanel } from './ChangesPanel';
import type { AuditLogItem } from '@/types/entities';

interface Props {
  initialItems: AuditLogItem[];
  initialCursor: string | null;
  hasMore: boolean;
  /** 현재 필터 — 더 보기 요청에 그대로 실어 같은 결과 집합을 유지. */
  filters: {
    range: string;
    group?: string;
    action?: string;
    actor_email?: string;
    resource_type?: string;
    resource_id?: string;
  };
  limit: number;
  /** i18n 라벨 해소 대상 — 카탈로그에 있는 액션만 t() 한다. */
  knownActions: string[];
}

/** 액션 표시 색 — 삭제/폐기/해제 계열과 민감 열람·확인됨을 시각적으로 구분. */
function actionTone(action: string): 'pink' | 'amber' | 'neutral' {
  if (
    action.startsWith('DELETE_') ||
    action.startsWith('REVOKE') ||
    action.startsWith('CLEAR_') ||
    action.startsWith('UNMARK_') ||
    action.startsWith('UNSET_')
  ) {
    return 'pink';
  }
  if (
    action === 'SET_BODY_LOGGING' ||
    action === 'VIEW_INVOCATION_LOG_BODY' ||
    action.endsWith('_CONFIRMED')
  ) {
    return 'amber';
  }
  return 'neutral';
}

export function AuditLogListView({
  initialItems,
  initialCursor,
  hasMore: initialHasMore,
  filters,
  limit,
  knownActions,
}: Props) {
  const t = useTranslations('audit');
  const locale = useLocale();
  const tz = useReportingTz();
  const { toast } = useToast();
  const [items, setItems] = useState(initialItems);
  const [cursor, setCursor] = useState(initialCursor);
  const [hasMore, setHasMore] = useState(initialHasMore);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [isPending, startTransition] = useTransition();

  const known = new Set(knownActions);
  // 라벨은 정규화된 이름으로 찾는다 — *_CONFIRMED 행은 원본 라벨 + 확인됨 배지.
  const normalize = (a: string) =>
    a.endsWith('_CONFIRMED') ? a.slice(0, -'_CONFIRMED'.length) : a;
  const actionLabel = (a: string) => {
    const base = normalize(a);
    return known.has(base) ? t(`actions.${base}` as const) : base;
  };

  function handleLoadMore() {
    startTransition(async () => {
      const result = await listAuditLogsAction({ cursor, ...filters, limit });
      if (!result.success) {
        toast({
          type: 'error',
          message: result.error ?? t('loadMoreFailed'),
          auto_dismiss_ms: 5000,
        });
        return;
      }
      setItems((prev) => [...prev, ...result.data.items]);
      setCursor(result.data.pagination.cursor);
      setHasMore(result.data.pagination.has_more);
    });
  }

  function toggle(id: string) {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function actorCell(it: AuditLogItem) {
    if (it.actor_kind === 'service_token') {
      return it.service_token ? (
        <span>
          {it.service_token.name}{' '}
          <span className="font-mono text-muted-foreground">
            {it.service_token.token_prefix}…
          </span>
        </span>
      ) : (
        <span className="text-muted-foreground">{t('actorServiceToken')}</span>
      );
    }
    if (it.actor_kind === 'dev') {
      return <span className="text-muted-foreground">{t('actorDev')}</span>;
    }
    if (it.actor_email) {
      return it.actor_email;
    }
    return (
      <span className="font-mono text-muted-foreground">
        {it.actor_user_id.slice(0, 8)}…
      </span>
    );
  }

  return (
    <div className="glass rounded-apple p-4">
      <Table>
        <THead>
          <Tr>
            <Th className="w-6"></Th>
            <Th>{t('colTime')}</Th>
            <Th>{t('colActor')}</Th>
            <Th>{t('colAction')}</Th>
            <Th>{t('colTarget')}</Th>
            <Th>{t('colResult')}</Th>
          </Tr>
        </THead>
        <TBody>
          {items.map((it) => (
            <Fragment key={it.id}>
              <Tr
                clickable
                onClick={() => toggle(it.id)}
                // 키보드 — 행 시맨틱(tr)을 유지한 채 Enter/Space 로 펼침.
                tabIndex={0}
                aria-expanded={expanded.has(it.id)}
                className="focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring focus-visible:ring-inset"
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault();
                    toggle(it.id);
                  }
                }}
              >
                <Td className="w-6 text-muted-foreground">
                  {expanded.has(it.id) ? (
                    <ChevronDown size={14} />
                  ) : (
                    <ChevronRight size={14} />
                  )}
                </Td>
                <Td className="whitespace-nowrap text-muted-foreground" title={it.timestamp}>
                  {fmtDateTime(it.timestamp, locale === 'ko' ? 'ko-KR' : 'en-US', tz)}
                </Td>
                <Td>
                  <div className="flex items-center gap-2">
                    <span className="truncate">{actorCell(it)}</span>
                    <Badge tone={it.actor_role === 'ADMIN' ? 'teal' : 'sky'}>
                      {it.actor_role}
                    </Badge>
                  </div>
                </Td>
                <Td>
                  <span title={it.action} className="inline-flex items-center gap-1.5">
                    <Badge tone={actionTone(it.action)}>{actionLabel(it.action)}</Badge>
                    {it.action.endsWith('_CONFIRMED') && (
                      <Badge tone="amber">{t('confirmedBadge')}</Badge>
                    )}
                  </span>
                </Td>
                <Td>
                  {/* 대상 클릭 → 그 리소스의 이력 필터로 새 컨텍스트(range 만 유지). */}
                  <Link
                    href={`/audit?${new URLSearchParams({
                      range: filters.range,
                      resource_type: it.resource_type,
                      resource_id: it.resource_id,
                    }).toString()}`}
                    onClick={(e) => e.stopPropagation()}
                    className="hover:underline"
                  >
                    <span className="text-muted-foreground">{it.resource_type}</span>{' '}
                    <span className="font-mono" title={it.resource_id}>
                      {it.resource_id.slice(0, 12)}
                      {it.resource_id.length > 12 ? '…' : ''}
                    </span>
                  </Link>
                </Td>
                <Td>
                  {it.result === 'SUCCESS' ? (
                    <Badge tone="neutral">{t('resultSuccess')}</Badge>
                  ) : (
                    <span title={it.result}>
                      <Badge tone="pink">{t('resultFailure')}</Badge>
                    </span>
                  )}
                </Td>
              </Tr>
              {expanded.has(it.id) && (
                <Tr>
                  <Td colSpan={6} className="bg-muted/30">
                    <ChangesPanel item={it} />
                  </Td>
                </Tr>
              )}
            </Fragment>
          ))}
          {items.length === 0 && <TEmpty colSpan={6}>{t('empty')}</TEmpty>}
        </TBody>
      </Table>

      {hasMore && (
        <div className="mt-4 flex justify-center">
          <button
            onClick={handleLoadMore}
            disabled={isPending}
            className="rounded-md border border-border bg-background px-4 py-2 text-sm hover:bg-accent disabled:opacity-50"
          >
            {isPending ? t('loading') : t('loadMore')}
          </button>
        </div>
      )}
    </div>
  );
}
