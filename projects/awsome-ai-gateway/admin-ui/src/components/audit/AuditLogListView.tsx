'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Fragment, useState, useTransition } from 'react';
import Link from 'next/link';
import { useLocale, useTranslations } from 'next-intl';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { listAuditLogsAction } from '@/lib/actions/audit';
import { baseAction, isConfirmedAction } from '@/lib/audit/action';
import { useToast } from '@/components/common/ToastProvider';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';
import { Badge, RoleBadge } from '@/components/common/Badge';
import { Table, THead, TBody, Tr, Th, Td, TEmpty } from '@/components/common/Table';
import { Button } from '@/components/common/Button';
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

/** 액션 표시 색 — 파괴 계열(danger)과 민감 열람·확인됨(amber)을 시각적으로 구분. */
function actionTone(action: string): 'danger' | 'amber' | 'neutral' {
  if (
    action.startsWith('DELETE_') ||
    action.startsWith('REVOKE') ||
    action.startsWith('CLEAR_') ||
    action.startsWith('UNMARK_') ||
    action.startsWith('UNSET_')
  ) {
    return 'danger';
  }
  if (
    action === 'SET_BODY_LOGGING' ||
    action === 'VIEW_BODY_LOG' ||
    action === 'VIEW_INVOCATION_LOG_BODY' ||
    isConfirmedAction(action)
  ) {
    return 'amber';
  }
  return 'neutral';
}

/** 액션을 배지가 아닌 텍스트 색으로 — 50행 pill 나열에서 실패·파괴만 눈에 띄게. */
const ACTION_TEXT_CLASS: Record<string, string> = {
  danger: 'text-destructive',
  amber: 'text-amber-700 dark:text-amber-400',
  neutral: '',
};

export function AuditLogListView({
  initialItems,
  initialCursor,
  hasMore: initialHasMore,
  filters,
  limit,
  knownActions,
}: Props) {
  const t = useTranslations('audit');
  const tAuth = useTranslations('auth');
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
  // t.has 가드: 카탈로그에만 있고 번역이 빠진 액션은 키 경로가 아니라 enum 원문을 둔다.
  const actionLabel = (a: string) => {
    const base = baseAction(a);
    const key = `actions.${base}` as const;
    return known.has(base) && t.has(key) ? t(key) : base;
  };
  const resourceLabel = (rt: string) => {
    const key = `resourceTypes.${rt}` as const;
    return t.has(key) ? t(key) : rt;
  };
  const roleLabel = (role: string) => {
    const key = `role.${role}` as const;
    return tAuth.has(key) ? tAuth(key) : role;
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
    // '<uuid>@unknown' 는 이메일 해석 실패 sentinel — 이메일처럼 보이지만 식별자다.
    if (it.actor_email && !it.actor_email.endsWith('@unknown')) {
      return it.actor_email;
    }
    return (
      <span title={it.actor_user_id}>
        <span className="text-muted-foreground">{t('unknownUser')}</span>{' '}
        <span className="font-mono text-muted-foreground">
          {it.actor_user_id.slice(0, 8)}…
        </span>
      </span>
    );
  }

  return (
    <div className="glass rounded-apple p-4">
      <Table density="compact">
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
              <Tr clickable onClick={() => toggle(it.id)}>
                <Td className="w-6 text-muted-foreground">
                  {/* 펼침은 행이 아니라 이 버튼이 담당 — tr 에 keydown 을 달면
                      내부 링크의 Enter 도 가로채 버린다(버블링). 버튼의 네이티브
                      활성화는 자기 자신만 건드린다. */}
                  <button
                    type="button"
                    aria-expanded={expanded.has(it.id)}
                    aria-label={
                      expanded.has(it.id)
                        ? t('collapseDetails')
                        : t('expandDetails')
                    }
                    onClick={(e) => {
                      e.stopPropagation();
                      toggle(it.id);
                    }}
                    className="rounded p-0.5 hover:bg-primary/10 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  >
                    {expanded.has(it.id) ? (
                      <ChevronDown size={14} />
                    ) : (
                      <ChevronRight size={14} />
                    )}
                  </button>
                </Td>
                <Td className="num whitespace-nowrap text-muted-foreground" title={it.timestamp}>
                  {fmtDateTime(it.timestamp, locale === 'ko' ? 'ko-KR' : 'en-US', tz)}
                </Td>
                <Td>
                  <div className="flex items-center gap-2">
                    <span className="truncate">{actorCell(it)}</span>
                    <RoleBadge role={it.actor_role} label={roleLabel(it.actor_role)} />
                  </div>
                </Td>
                <Td>
                  {/* 액션은 텍스트 색으로만 구분 — 배지 3~4개 나열에서 실패가
                      묻히는 걸 막는다(확인됨만 배지 유지). */}
                  <span title={it.action} className="inline-flex items-center gap-1.5">
                    <span className={`font-medium ${ACTION_TEXT_CLASS[actionTone(it.action)]}`}>
                      {actionLabel(it.action)}
                    </span>
                    {isConfirmedAction(it.action) && (
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
                    <span className="text-muted-foreground" title={it.resource_type}>
                      {resourceLabel(it.resource_type)}
                    </span>{' '}
                    <span className="font-mono" title={it.resource_id}>
                      {it.resource_id.slice(0, 12)}
                      {it.resource_id.length > 12 ? '…' : ''}
                    </span>
                  </Link>
                </Td>
                <Td>
                  {/* 성공은 감사의 기본값 — 매 행 배지를 달면 실패가 안 보인다. */}
                  {it.result === 'SUCCESS' ? (
                    <span className="text-muted-foreground">—</span>
                  ) : (
                    <span title={it.result}>
                      <Badge tone="danger">{t('resultFailure')}</Badge>
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
          <Button variant="ghost" onClick={handleLoadMore} disabled={isPending}>
            {isPending ? t('loading') : t('loadMore')}
          </Button>
        </div>
      )}
    </div>
  );
}
