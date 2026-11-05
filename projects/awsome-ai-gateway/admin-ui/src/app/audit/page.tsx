// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import Link from 'next/link';
import { getTranslations } from 'next-intl/server';
import { adminAPI } from '@/lib/api-client';
import { baseAction } from '@/lib/audit/action';
import { APIError } from '@/lib/utils/retry';
import { ErrorState } from '@/components/common/ErrorState';
import { AuditLogListView } from '@/components/audit/AuditLogListView';
import { ActionSelect } from '@/components/audit/ActionSelect';
import type {
  AuditActionCatalogResponse,
  AuditLogListResponse,
} from '@/types/entities';

export const dynamic = 'force-dynamic';

const PAGE_LIMIT = 50;
const RANGES = ['24h', '7d', '30d'] as const;

interface AuditPageProps {
  searchParams?: Promise<{
    range?: string;
    group?: string;
    action?: string;
    actor_email?: string;
    resource_type?: string;
    resource_id?: string;
  }>;
}

export default async function AuditPage(props: AuditPageProps) {
  const searchParams = await props.searchParams;
  const t = await getTranslations('audit');

  const range = RANGES.includes(searchParams?.range as (typeof RANGES)[number])
    ? (searchParams!.range as (typeof RANGES)[number])
    : '7d';
  const group = searchParams?.group ?? '';
  const action = searchParams?.action ?? '';
  const actorEmail = searchParams?.actor_email?.trim() ?? '';
  const resourceType = searchParams?.resource_type ?? '';
  const resourceId = searchParams?.resource_id ?? '';

  const fetchLogs = (g: string, a: string) =>
    adminAPI
      .get<AuditLogListResponse>('/admin/audit/logs', {
        range,
        limit: PAGE_LIMIT,
        ...(g ? { group: g } : {}),
        ...(a ? { action: a } : {}),
        ...(actorEmail ? { actor_email: actorEmail } : {}),
        ...(resourceType ? { resource_type: resourceType } : {}),
        ...(resourceId ? { resource_id: resourceId } : {}),
      })
      .then((v) => ({ ok: true as const, value: v }))
      .catch((err) => ({
        ok: false as const,
        // 400(검색어 500명 초과·잘못된 커서) 같은 실천 가능한 사유만 살린다 —
        // 5xx 서버 메시지는 내부 정보를 담을 수 있어 원문 노출하지 않는다.
        // 필터 UI 는 유지한다 — 통째 ErrorState 로 덮으면 원인이 안 보인다.
        error:
          err instanceof APIError && err.status === 400
            ? err.message
            : undefined,
      }));

  // 카탈로그를 먼저 받아 group 을 검증한 뒤 로그는 한 번만 가져온다 —
  // 잘못된 group 은 가져오기 전에 버려 pill/필터/더보기가 항상 같은
  // 조건을 쓴다(임의 실패를 무필터 재조회로 숨기지 않는다).
  const catalogResult = await adminAPI
    .get<AuditActionCatalogResponse>('/admin/audit/action-catalog')
    .then((v) => ({ ok: true as const, value: v }))
    .catch(() => ({ ok: false as const }));

  if (!catalogResult.ok) {
    return <ErrorState />;
  }

  const groups = catalogResult.value.groups;
  const groupKeys = new Set(groups.map((g) => g.key));
  const activeGroup = groupKeys.has(group) ? group : '';
  const logsResult = await fetchLogs(activeGroup, action);

  const groupActions = activeGroup
    ? (groups.find((g) => g.key === activeGroup)?.actions ?? [])
    : [];
  const knownActions = groups.flatMap((g) => g.actions);

  // 필터 링크용 공통 쿼리 조립 — 현재 상태 보존 + 변경분만 덮어쓴다.
  const buildQuery = (over: Record<string, string>) => {
    const sp = new URLSearchParams();
    sp.set('range', range);
    if (activeGroup) sp.set('group', activeGroup);
    if (action) sp.set('action', action);
    if (actorEmail) sp.set('actor_email', actorEmail);
    if (resourceType) sp.set('resource_type', resourceType);
    if (resourceId) sp.set('resource_id', resourceId);
    for (const [k, v] of Object.entries(over)) {
      if (v) sp.set(k, v);
      else sp.delete(k);
    }
    return `/audit?${sp.toString()}`;
  };

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">{t('pageTitle')}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('pageDescription')}
        </p>
      </div>

      {/* 기간 pill — 상대 구간(24h/7d/30d). 백엔드도 필수로 받아 스캔 범위를 묶는다. */}
      <div className="mb-4 flex flex-wrap items-center gap-2">
        {RANGES.map((r) => (
          <Link
            key={r}
            href={buildQuery({ range: r })}
            aria-current={r === range ? 'page' : undefined}
            className={`inline-flex items-center rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${
              r === range
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-background text-foreground border-border hover:bg-accent'
            }`}
          >
            {t(`range.${r}` as const)}
          </Link>
        ))}

        <span className="mx-1 h-5 w-px bg-border" />

        {/* 액션 그룹 pill — 그룹 변경 시 세부 action 은 리셋(다른 그룹 액션이 남지 않게). */}
        <Link
          href={buildQuery({ group: '', action: '' })}
          className={`inline-flex items-center rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${
            !activeGroup
              ? 'bg-primary text-primary-foreground border-primary'
              : 'bg-background text-foreground border-border hover:bg-accent'
          }`}
        >
          {t('groupAll')}
        </Link>
        {groups.map((g) => (
          <Link
            key={g.key}
            href={buildQuery({ group: g.key, action: '' })}
            aria-current={g.key === activeGroup ? 'page' : undefined}
            className={`inline-flex items-center rounded-md border px-3 py-1.5 text-sm font-medium transition-colors ${
              g.key === activeGroup
                ? 'bg-primary text-primary-foreground border-primary'
                : 'bg-background text-foreground border-border hover:bg-accent'
            }`}
          >
            {t(`groups.${g.key}` as const)}
          </Link>
        ))}

        {groupActions.length > 0 && (
          <ActionSelect
            actions={groupActions}
            currentAction={action}
            knownActions={knownActions}
          />
        )}
      </div>

      {/* 행위자 이메일 검색 + 대상 칩 */}
      <div className="mb-6 flex flex-wrap items-center gap-2">
        <form method="GET" className="flex items-center gap-2">
          {/* 나머지 필터를 hidden 으로 실어 submit 이 리셋하지 않게 한다. */}
          <input type="hidden" name="range" value={range} />
          {activeGroup && <input type="hidden" name="group" value={activeGroup} />}
          {action && <input type="hidden" name="action" value={action} />}
          {resourceType && (
            <input type="hidden" name="resource_type" value={resourceType} />
          )}
          {resourceId && (
            <input type="hidden" name="resource_id" value={resourceId} />
          )}
          <input
            type="search"
            name="actor_email"
            defaultValue={actorEmail}
            placeholder={t('actorPlaceholder')}
            className="w-64 rounded-md border border-border bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <button
            type="submit"
            className="inline-flex items-center rounded-md border border-border bg-background px-3 py-2 text-sm font-medium hover:bg-accent transition-colors"
          >
            {t('searchButton')}
          </button>
          {actorEmail && (
            <Link
              href={buildQuery({ actor_email: '' })}
              className="text-sm text-muted-foreground hover:text-foreground underline-offset-4 hover:underline"
            >
              {t('resetSearch')}
            </Link>
          )}
        </form>

        {/* 활성 액션 필터 — ?action=X 딥링크(group 없음)도 화면에 보이고 해제된다. */}
        {action && (
          <span className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-600 dark:text-amber-400">
            {t('actionChip', {
              action: knownActions.includes(baseAction(action))
                ? t(`actions.${baseAction(action)}` as const)
                : action,
            })}
            <Link
              href={buildQuery({ action: '' })}
              aria-label={t('clearAction')}
              className="font-bold hover:text-amber-800"
            >
              ✕
            </Link>
          </span>
        )}
        {(resourceType || resourceId) && (
          <span className="inline-flex items-center gap-1.5 rounded-md border border-amber-500/40 bg-amber-500/10 px-2.5 py-1.5 text-xs text-amber-600 dark:text-amber-400">
            {t('targetFilter', { type: resourceType || resourceId })}
            <Link
              href={buildQuery({ resource_type: '', resource_id: '' })}
              aria-label={t('clearTarget')}
              className="font-bold hover:text-amber-800"
            >
              ✕
            </Link>
          </span>
        )}
      </div>

      {!logsResult.ok ? (
        <div className="glass rounded-apple p-6">
          <p className="text-sm font-medium text-destructive">{t('listLoadFailed')}</p>
          {logsResult.error && (
            <p className="mt-1 text-xs text-muted-foreground">{logsResult.error}</p>
          )}
        </div>
      ) : (
        /* key=필터 조합 — 필터 변경 시 remount해 이전 목록이 남지 않는다(KeysListView 관례). */
        <AuditLogListView
          key={`${range}:${activeGroup}:${action}:${actorEmail}:${resourceType}:${resourceId}`}
          initialItems={logsResult.value.items}
          initialCursor={logsResult.value.pagination.cursor}
          hasMore={logsResult.value.pagination.has_more}
          filters={{
            range,
            ...(activeGroup ? { group: activeGroup } : {}),
            ...(action ? { action } : {}),
            ...(actorEmail ? { actor_email: actorEmail } : {}),
            ...(resourceType ? { resource_type: resourceType } : {}),
            ...(resourceId ? { resource_id: resourceId } : {}),
          }}
          limit={PAGE_LIMIT}
          knownActions={knownActions}
        />
      )}
    </div>
  );
}
