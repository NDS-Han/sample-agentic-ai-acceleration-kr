'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Budget Management 의 "웹 검색 한도" 섹션 — 조직 기본·팀 공용 풀·사용자 개별 지정 + 이번 달 사용.
 *
 * **검색 수 기준이며 예산과 별개다.** 한도에 닿아도 모델 호출은 막히지 않는다(게이트웨이가
 * web_search 도구만 빼는 soft block). 빈 칸 = 상속(조직은 무제한/게이트웨이 기본 상한),
 * 월 한도 0 = 사용 안 함. 변경은 게이트웨이 정책 캐시(약 30초) 뒤 반영된다.
 */

import { useEffect, useMemo, useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';

import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { UsageBar } from '@/components/budgets/budgetVisuals';
import { alertLevelOf } from '@/lib/utils/alertLevel';
import { clearWebSearchQuotaAction, setWebSearchQuotaAction } from '@/lib/actions/webSearchQuota';
import { searchUsersAction } from '@/lib/actions/users';
import type {
  UserSearchItem,
  WebSearchPolicyValues,
  WebSearchQuotaOverview,
} from '@/types/entities';

type Scope = 'org' | 'teams' | 'users';

/** '' → null(상속), 정수 문자열 → 숫자, 그 외 → undefined(오류). */
export function parseQuotaInput(raw: string, min: number, max: number): number | null | undefined {
  const v = raw.trim();
  if (v === '') return null;
  if (!/^\d+$/.test(v)) return undefined;
  const n = Number(v);
  return n >= min && n <= max ? n : undefined;
}

function UsedCell({ used, limit }: { used: number; limit: number | null }) {
  const t = useTranslations('webSearchQuota');
  if (limit == null) return <span className="tabular-nums">{t('usedOnly', { used })}</span>;
  const pct = limit > 0 ? (used / limit) * 100 : 100;
  return (
    <div className="min-w-[8rem] space-y-1" data-testid="ws-quota-used">
      <span className="tabular-nums text-xs">{t('usedOf', { used, limit })}</span>
      <UsageBar pct={Math.min(pct, 100)} level={alertLevelOf(pct)} />
    </div>
  );
}

function ValueText({ value, fallback }: { value: number | null; fallback: string }) {
  const t = useTranslations('webSearchQuota');
  if (value == null) return <span className="text-muted-foreground">{fallback}</span>;
  if (value === 0) return <span className="text-destructive">{t('off')}</span>;
  return <span className="tabular-nums">{value.toLocaleString('en-US')}</span>;
}

/** 한 행(조직·팀·사용자)의 보기/편집. */
function PolicyEditor({
  scope,
  id,
  values,
  limitLabel,
  limitFallback,
  capFallback,
  onSaved,
  onCleared,
  startEditing = false,
  onCancel,
}: {
  scope: Scope;
  id: string | null;
  values: WebSearchPolicyValues | null;
  limitLabel: string;
  limitFallback: string;
  capFallback: string;
  onSaved: (_v: WebSearchPolicyValues) => void;
  onCleared: () => void;
  startEditing?: boolean;
  onCancel?: () => void;
}) {
  const t = useTranslations('webSearchQuota');
  const { toast } = useToast();
  const [editing, setEditing] = useState(startEditing);
  const [limit, setLimit] = useState(values?.monthly_query_limit?.toString() ?? '');
  const [cap, setCap] = useState(values?.per_request_cap?.toString() ?? '');
  const [isPending, startTransition] = useTransition();
  const limitVal = parseQuotaInput(limit, 0, 10_000_000);
  const capVal = parseQuotaInput(cap, 1, 50);
  const valid = limitVal !== undefined && capVal !== undefined;

  const save = () => {
    if (!valid) return;
    startTransition(async () => {
      const body = { monthly_query_limit: limitVal ?? null, per_request_cap: capVal ?? null };
      const r = await setWebSearchQuotaAction(scope, id, body);
      if (r.success) {
        toast({ type: 'success', message: t('saved'), auto_dismiss_ms: 3000 });
        setEditing(false);
        onSaved(body);
      } else {
        toast({ type: 'error', message: `${t('saveFailed')} ${r.error}`, auto_dismiss_ms: 4000 });
      }
    });
  };
  const clear = () => {
    startTransition(async () => {
      const r = await clearWebSearchQuotaAction(scope, id);
      if (r.success) {
        toast({ type: 'success', message: t('cleared'), auto_dismiss_ms: 3000 });
        setLimit('');
        setCap('');
        setEditing(false);
        onCleared();
      } else {
        toast({ type: 'error', message: `${t('saveFailed')} ${r.error}`, auto_dismiss_ms: 4000 });
      }
    });
  };

  if (!editing) {
    return (
      <div className="flex flex-wrap items-center justify-end gap-x-4 gap-y-1 text-sm">
        <span>
          <span className="text-xs text-muted-foreground">{limitLabel}: </span>
          <ValueText value={values?.monthly_query_limit ?? null} fallback={limitFallback} />
        </span>
        <span>
          <span className="text-xs text-muted-foreground">{t('perRequestCap')}: </span>
          <ValueText value={values?.per_request_cap ?? null} fallback={capFallback} />
        </span>
        <button type="button" className="text-xs text-primary hover:underline" onClick={() => setEditing(true)}>
          {t('edit')}
        </button>
        {values && (
          <button type="button" className="text-xs text-destructive hover:underline" onClick={clear}
            disabled={isPending}>
            {t('clear')}
          </button>
        )}
      </div>
    );
  }
  const inputCls =
    'w-28 rounded-apple-md border border-input bg-background px-2 py-1 text-sm tabular-nums focus:outline-none focus:ring-1 focus:ring-ring';
  return (
    <form
      className="flex flex-wrap items-end justify-end gap-2"
      onSubmit={(e) => {
        e.preventDefault();
        save();
      }}
    >
      <label className="flex flex-col gap-0.5 text-xs">
        {limitLabel}
        <input className={inputCls} inputMode="numeric" value={limit} placeholder={limitFallback}
          onChange={(e) => setLimit(e.target.value)} aria-invalid={limitVal === undefined || undefined} />
      </label>
      <label className="flex flex-col gap-0.5 text-xs">
        {t('perRequestCap')}
        <input className={inputCls} inputMode="numeric" value={cap} placeholder={capFallback}
          onChange={(e) => setCap(e.target.value)} aria-invalid={capVal === undefined || undefined} />
      </label>
      <SpinnerButton type="submit" size="sm" isLoading={isPending} disabled={!valid}>
        {t('save')}
      </SpinnerButton>
      <button
        type="button"
        className="text-xs text-muted-foreground hover:underline"
        onClick={() => {
          setEditing(false);
          onCancel?.();
        }}
      >
        {t('cancel')}
      </button>
      {!valid && (
        <p role="alert" className="basis-full text-right text-xs text-destructive">{t('invalid')}</p>
      )}
    </form>
  );
}

function AddUserOverride({ onPick }: { onPick: (_u: UserSearchItem) => void }) {
  const t = useTranslations('webSearchQuota');
  const [term, setTerm] = useState('');
  const [items, setItems] = useState<UserSearchItem[] | null>(null);
  useEffect(() => {
    if (term.trim().length < 2) {
      setItems(null);
      return;
    }
    let alive = true;
    const h = setTimeout(async () => {
      const r = await searchUsersAction(term.trim());
      if (alive) setItems(r.success ? r.data.items : []);
    }, 250);
    return () => {
      alive = false;
      clearTimeout(h);
    };
  }, [term]);
  return (
    <div className="relative w-full max-w-sm">
      <input
        aria-label={t('searchUser')}
        placeholder={t('searchUser')}
        value={term}
        onChange={(e) => setTerm(e.target.value)}
        className="w-full rounded-apple-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
      />
      {items && (
        <ul className="absolute z-20 mt-1 max-h-56 w-full overflow-auto rounded-md border border-border bg-popover py-1 text-sm shadow-md">
          {items.length === 0 && <li className="px-3 py-1.5 text-muted-foreground">{t('noUserMatch')}</li>}
          {items.map((u) => (
            <li key={u.id}>
              <button
                type="button"
                className="w-full px-3 py-1.5 text-left hover:bg-accent"
                onClick={() => {
                  onPick(u);
                  setTerm('');
                  setItems(null);
                }}
              >
                {u.display_name ? `${u.display_name} · ` : ''}
                {u.email}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function WebSearchQuotaSection({ initial }: { initial: WebSearchQuotaOverview | null }) {
  const t = useTranslations('webSearchQuota');
  const [data, setData] = useState(initial);
  const [adding, setAdding] = useState<UserSearchItem | null>(null);
  const orgLimit = data?.org?.monthly_query_limit ?? null;
  const defaultCapText = data ? t('defaultCap', { cap: data.default_per_request_cap }) : '';
  const orgCapText =
    data?.org?.per_request_cap != null ? t('defaultCap', { cap: data.org.per_request_cap }) : defaultCapText;
  const userFallback = orgLimit != null ? `${t('inherit')} (${orgLimit.toLocaleString('en-US')})` : t('unlimited');
  const teams = useMemo(
    () => [...(data?.teams ?? [])].sort((a, b) =>
      Number(b.monthly_query_limit != null) - Number(a.monthly_query_limit != null) || b.used - a.used),
    [data],
  );

  if (!data) {
    return (
      <section id="web-search-quota" className="glass rounded-apple p-5" data-testid="web-search-quota">
        <h2 className="text-base font-semibold">{t('sectionTitle')}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{t('loadFailed')}</p>
      </section>
    );
  }
  const patch = (fn: (_d: WebSearchQuotaOverview) => WebSearchQuotaOverview) =>
    setData((d) => (d ? fn(d) : d));

  return (
    <section id="web-search-quota" className="glass rounded-apple p-5 space-y-5" data-testid="web-search-quota">
      <div>
        <h2 className="text-base font-semibold">{t('sectionTitle')}</h2>
        <p className="mt-1 text-xs text-muted-foreground">{t('sectionDesc')}</p>
      </div>

      {/* 조직 기본 */}
      <div className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-border/60 bg-muted/30 p-3"
        data-testid="ws-quota-org">
        <div>
          <p className="text-sm font-medium">{t('orgTitle')}</p>
          <p className="text-xs text-muted-foreground">{t('orgDesc')}</p>
          <p className="mt-1 text-xs text-muted-foreground">{t('orgUsed', { count: data.org_used })}</p>
        </div>
        <PolicyEditor
          scope="org"
          id={null}
          values={data.org}
          limitLabel={t('perUserLimit')}
          limitFallback={t('unlimited')}
          capFallback={defaultCapText}
          onSaved={(v) => patch((d) => ({ ...d, org: { ...v, updated_at: null } }))}
          onCleared={() => patch((d) => ({ ...d, org: null }))}
        />
      </div>

      {/* 팀 공용 풀 */}
      <div className="space-y-2">
        <div>
          <h3 className="text-sm font-semibold">{t('teamsTitle')}</h3>
          <p className="text-xs text-muted-foreground">{t('teamsDesc')}</p>
        </div>
        <ul className="divide-y divide-border/60" data-testid="ws-quota-teams">
          {teams.map((tm) => {
            const has = tm.monthly_query_limit != null || tm.per_request_cap != null;
            return (
              <li key={tm.team_id} className="flex flex-wrap items-center justify-between gap-3 py-2">
                <div className="min-w-0">
                  <p className="truncate text-sm font-medium">{tm.team_name}</p>
                  {tm.dept_name && <p className="text-xs text-muted-foreground">{tm.dept_name}</p>}
                </div>
                <UsedCell used={tm.used} limit={tm.monthly_query_limit} />
                <PolicyEditor
                  scope="teams"
                  id={tm.team_id}
                  values={has ? tm : null}
                  limitLabel={t('teamPoolLimit')}
                  limitFallback={t('unlimited')}
                  capFallback={orgCapText}
                  onSaved={(v) => patch((d) => ({ ...d, teams: d.teams.map((x) =>
                    x.team_id === tm.team_id ? { ...x, ...v } : x) }))}
                  onCleared={() => patch((d) => ({ ...d, teams: d.teams.map((x) =>
                    x.team_id === tm.team_id ? { ...x, monthly_query_limit: null, per_request_cap: null } : x) }))}
                />
              </li>
            );
          })}
        </ul>
      </div>

      {/* 사용자 개별 지정 */}
      <div className="space-y-2">
        <h3 className="text-sm font-semibold">{t('usersTitle')}</h3>
        <ul className="divide-y divide-border/60" data-testid="ws-quota-users">
          {data.users.length === 0 && !adding && (
            <li className="py-2 text-xs text-muted-foreground">{t('usersEmpty')}</li>
          )}
          {data.users.map((u) => (
            <li key={u.user_id} className="flex flex-wrap items-center justify-between gap-3 py-2">
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">{u.display_name ?? u.email}</p>
                <p className="truncate text-xs text-muted-foreground">
                  {u.email}
                  {u.team_name ? ` · ${u.team_name}` : ''}
                </p>
              </div>
              <UsedCell used={u.used} limit={u.monthly_query_limit ?? orgLimit} />
              <PolicyEditor
                scope="users"
                id={u.user_id}
                values={u}
                limitLabel={t('monthlyLimit')}
                limitFallback={userFallback}
                capFallback={orgCapText}
                onSaved={(v) => patch((d) => ({ ...d, users: d.users.map((x) =>
                  x.user_id === u.user_id ? { ...x, ...v } : x) }))}
                onCleared={() => patch((d) => ({ ...d, users: d.users.filter((x) => x.user_id !== u.user_id) }))}
              />
            </li>
          ))}
          {adding && (
            <li className="flex flex-wrap items-center justify-between gap-3 py-2">
              <p className="text-sm font-medium">{adding.display_name ?? adding.email}</p>
              <PolicyEditor
                scope="users"
                id={adding.id}
                values={null}
                limitLabel={t('monthlyLimit')}
                limitFallback={userFallback}
                capFallback={orgCapText}
                startEditing
                onCancel={() => setAdding(null)}
                onSaved={(v) => {
                  patch((d) => ({ ...d, users: [...d.users.filter((x) => x.user_id !== adding.id), {
                    user_id: adding.id, email: adding.email, display_name: adding.display_name ?? null,
                    team_name: adding.team_name ?? null, used: 0, ...v }] }));
                  setAdding(null);
                }}
                onCleared={() => setAdding(null)}
              />
            </li>
          )}
        </ul>
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium">{t('addOverride')}</span>
          <AddUserOverride onPick={setAdding} />
        </div>
      </div>
    </section>
  );
}
