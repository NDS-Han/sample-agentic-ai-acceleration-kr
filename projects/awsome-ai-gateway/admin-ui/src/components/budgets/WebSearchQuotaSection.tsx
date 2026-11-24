'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Budget Management 의 "웹 검색 한도" 섹션 — 위 예산 표와 같은 시각 언어.
 *
 *  1) 조직 요약 카드: 이번 달 검색 수(큰 숫자) · 사용자별 월 기본 한도 · 기본 요청당 상한 + [한도 설정]
 *  2) 팀 표: 대상 | 이번 달 검색 | 월 한도(팀 공용) | 사용률 | 요청당 상한 | 액션
 *  3) 사용자 개별 지정 표(같은 열) + 사용자 검색으로 추가
 *  4) 편집은 예산 대화상자와 같은 AppDialog — 단위가 붙은 입력, 상속값 placeholder,
 *     비우기(상속), 검증, Esc 취소, 저장/취소 나란히.
 *
 * **검색 수 기준이며 예산과 별개다.** 한도에 닿아도 모델 호출은 막히지 않는다(게이트웨이가
 * web_search 도구만 빼는 soft block). 빈 칸 = 상속, 월 한도 0 = 사용 안 함, 요청당 상한은
 * 1~50(API 제약). 변경은 게이트웨이 정책 캐시(약 30초) 뒤 반영된다.
 */

import { useEffect, useId, useMemo, useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';

import { AppDialog } from '@/components/common/AppDialog';
import { InfoTooltip } from '@/components/common/InfoTooltip';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { Table, TBody, Td, TEmpty, THead, Th, Tr } from '@/components/common/Table';
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

export const MONTHLY_MAX = 10_000_000;
export const CAP_MIN = 1;
export const CAP_MAX = 50;

/** '' → null(상속), 정수 문자열 → 숫자, 그 외 → undefined(오류). */
export function parseQuotaInput(raw: string, min: number, max: number): number | null | undefined {
  const v = raw.trim();
  if (v === '') return null;
  if (!/^\d+$/.test(v)) return undefined;
  const n = Number(v);
  return n >= min && n <= max ? n : undefined;
}

/** 사용률(%) — 한도 없음(null)·사용 안 함(0)은 null('—'). */
export function quotaUsagePct(used: number, limit: number | null): number | null {
  if (limit == null || limit <= 0) return null;
  return (used / limit) * 100;
}

/** 예산 표의 "예산 설정" 버튼과 같은 모양. */
const ACTION_BTN =
  'inline-flex items-center justify-center whitespace-nowrap rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring';

function LimitText({ value, inherited }: { value: number | null; inherited: string }) {
  const t = useTranslations('webSearchQuota');
  if (value == null) return <span className="text-muted-foreground">{inherited}</span>;
  if (value === 0) return <span className="text-destructive">{t('off')}</span>;
  return <span className="tabular-nums">{value.toLocaleString('en-US')}</span>;
}

function UsageCell({ used, limit }: { used: number; limit: number | null }) {
  const pct = quotaUsagePct(used, limit);
  if (pct == null) return <span className="text-xs text-muted-foreground">—</span>;
  return (
    <div className="flex items-center gap-2" data-testid="ws-quota-usage">
      <UsageBar pct={Math.min(pct, 100)} level={alertLevelOf(pct)} />
      <span className="w-12 text-right text-[11px] tabular-nums text-muted-foreground whitespace-nowrap">
        {pct.toFixed(1)}%
      </span>
    </div>
  );
}

function HeadWithHint({ label, hint, numeric }: { label: string; hint: string; numeric?: boolean }) {
  return (
    <Th numeric={numeric}>
      <span className="inline-flex items-center gap-1">
        {label}
        <InfoTooltip label={label}>{hint}</InfoTooltip>
      </span>
    </Th>
  );
}

// ─── 편집 대화상자 ───────────────────────────────────────────────────────────

export interface QuotaEditTarget {
  scope: Scope;
  id: string | null;
  /** 대화상자 제목에 들어갈 대상 이름. */
  name: string;
  values: WebSearchPolicyValues | null;
  monthlyLabel: string;
  /** 비웠을 때 적용되는 값(placeholder "상속: …"). */
  monthlyInherited: string;
  capInherited: string;
  /** 저장된 지정이 있으면 '지정 해제'(DELETE)를 보인다. */
  removable: boolean;
  /** 검색에서 고른, 아직 표에 없는 사용자 — 저장되면 이 신원으로 행을 추가한다. */
  newUser?: UserSearchItem;
}

function NumberField({
  label,
  unit,
  value,
  onChange,
  placeholder,
  hint,
  error,
  clearLabel,
}: {
  label: string;
  unit: string;
  value: string;
  onChange: (_v: string) => void;
  placeholder: string;
  hint: string;
  error: string | null;
  clearLabel: string;
}) {
  const id = useId();
  return (
    <div className="space-y-1">
      <label htmlFor={id} className="text-sm font-medium">
        {label}
      </label>
      <div className="flex items-center gap-2">
        <div className="relative flex-1">
          <input
            id={id}
            inputMode="numeric"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder}
            aria-invalid={error ? true : undefined}
            aria-describedby={`${id}-hint`}
            className="w-full rounded-md border border-input bg-background py-2 pl-3 pr-20 text-sm tabular-nums focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring aria-[invalid=true]:border-destructive"
          />
          <span className="pointer-events-none absolute inset-y-0 right-3 flex items-center text-xs text-muted-foreground">
            {unit}
          </span>
        </div>
        <button
          type="button"
          onClick={() => onChange('')}
          disabled={value === ''}
          className="whitespace-nowrap rounded-md px-2 py-2 text-xs text-muted-foreground hover:text-foreground hover:underline disabled:opacity-40 disabled:no-underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {clearLabel}
        </button>
      </div>
      <p id={`${id}-hint`} className={`text-xs ${error ? 'text-destructive' : 'text-muted-foreground'}`} role={error ? 'alert' : undefined}>
        {error ?? hint}
      </p>
    </div>
  );
}

export function QuotaDialog({
  target,
  onClose,
  onSaved,
  onCleared,
}: {
  target: QuotaEditTarget | null;
  onClose: () => void;
  onSaved: (_t: QuotaEditTarget, _v: WebSearchPolicyValues) => void;
  onCleared: (_t: QuotaEditTarget) => void;
}) {
  const t = useTranslations('webSearchQuota');
  const { toast } = useToast();
  const [limit, setLimit] = useState('');
  const [cap, setCap] = useState('');
  const [isPending, startTransition] = useTransition();

  useEffect(() => {
    setLimit(target?.values?.monthly_query_limit?.toString() ?? '');
    setCap(target?.values?.per_request_cap?.toString() ?? '');
  }, [target]);

  const limitVal = parseQuotaInput(limit, 0, MONTHLY_MAX);
  const capVal = parseQuotaInput(cap, CAP_MIN, CAP_MAX);
  const valid = limitVal !== undefined && capVal !== undefined;

  const save = () => {
    if (!target || !valid) return;
    startTransition(async () => {
      const body = { monthly_query_limit: limitVal ?? null, per_request_cap: capVal ?? null };
      const r = await setWebSearchQuotaAction(target.scope, target.id, body);
      if (r.success) {
        toast({ type: 'success', message: t('saved'), auto_dismiss_ms: 3000 });
        onSaved(target, body);
        onClose();
      } else {
        toast({ type: 'error', message: `${t('saveFailed')} ${r.error}`, auto_dismiss_ms: 4000 });
      }
    });
  };
  const remove = () => {
    if (!target) return;
    startTransition(async () => {
      const r = await clearWebSearchQuotaAction(target.scope, target.id);
      if (r.success) {
        toast({ type: 'success', message: t('cleared'), auto_dismiss_ms: 3000 });
        onCleared(target);
        onClose();
      } else {
        toast({ type: 'error', message: `${t('saveFailed')} ${r.error}`, auto_dismiss_ms: 4000 });
      }
    });
  };

  return (
    <AppDialog
      isOpen={!!target}
      onClose={onClose}
      title={target ? t('dialogTitle', { target: target.name }) : ''}
      description={t('dialogDesc')}
    >
      {target && (
        <form
          className="mt-4 space-y-4"
          onSubmit={(e) => {
            e.preventDefault();
            save();
          }}
        >
          <NumberField
            label={target.monthlyLabel}
            unit={t('unitMonth')}
            value={limit}
            onChange={setLimit}
            placeholder={t('inheritPlaceholder', { value: target.monthlyInherited })}
            hint={t('monthlyHint')}
            error={limitVal === undefined ? t('invalidMonthly') : null}
            clearLabel={t('clearToInherit')}
          />
          <NumberField
            label={t('perRequestCap')}
            unit={t('unitRequest')}
            value={cap}
            onChange={setCap}
            placeholder={t('inheritPlaceholder', { value: target.capInherited })}
            hint={t('capHint', { min: CAP_MIN, max: CAP_MAX })}
            error={capVal === undefined ? t('invalidCap', { min: CAP_MIN, max: CAP_MAX }) : null}
            clearLabel={t('clearToInherit')}
          />
          <div className="flex flex-wrap items-center justify-between gap-2 pt-2">
            {target.removable ? (
              <button
                type="button"
                onClick={remove}
                disabled={isPending}
                className="rounded-md px-2 py-2 text-sm text-destructive hover:underline disabled:opacity-50 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                {target.scope === 'org' ? t('resetOrg') : t('removeOverride')}
              </button>
            ) : (
              <span />
            )}
            <div className="flex items-center gap-2">
              <button
                type="button"
                onClick={onClose}
                disabled={isPending}
                className="inline-flex items-center justify-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
              >
                {t('cancel')}
              </button>
              <SpinnerButton type="submit" isLoading={isPending} disabled={!valid}>
                {t('save')}
              </SpinnerButton>
            </div>
          </div>
        </form>
      )}
    </AppDialog>
  );
}

// ─── 사용자 검색(개별 지정 추가) ──────────────────────────────────────────────

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
        type="search"
        aria-label={t('searchUser')}
        placeholder={t('searchUser')}
        value={term}
        onChange={(e) => setTerm(e.target.value)}
        className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
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

// ─── 섹션 ────────────────────────────────────────────────────────────────────

export function WebSearchQuotaSection({
  initial,
  teamNames,
}: {
  initial: WebSearchQuotaOverview | null;
  /** team_id → 위 예산 표와 같은 팀 표시 이름(budget summary 의 target_name). */
  teamNames?: Record<string, string>;
}) {
  const t = useTranslations('webSearchQuota');
  const [data, setData] = useState(initial);
  const [editing, setEditing] = useState<QuotaEditTarget | null>(null);
  const orgLimit = data?.org?.monthly_query_limit ?? null;
  const orgCap = data?.org?.per_request_cap ?? null;
  const gatewayCap = data?.default_per_request_cap ?? 0;
  /** 조직 밑(팀·사용자)이 상속받는 요청당 상한 문구. */
  const inheritedCapText = t('defaultCap', { cap: orgCap ?? gatewayCap });
  const orgLimitText = orgLimit == null ? t('unlimited') : orgLimit === 0 ? t('off') : orgLimit.toLocaleString('en-US');
  const teams = useMemo(
    () =>
      [...(data?.teams ?? [])].sort(
        (a, b) => Number(b.monthly_query_limit != null) - Number(a.monthly_query_limit != null) || b.used - a.used,
      ),
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
  const patch = (fn: (_d: WebSearchQuotaOverview) => WebSearchQuotaOverview) => setData((d) => (d ? fn(d) : d));
  const teamLabel = (tm: { team_id: string; team_name: string }) => teamNames?.[tm.team_id] ?? tm.team_name;

  const onSaved = (tg: QuotaEditTarget, v: WebSearchPolicyValues) => {
    if (tg.scope === 'org') patch((d) => ({ ...d, org: { ...v, updated_at: null } }));
    else if (tg.scope === 'teams')
      patch((d) => ({ ...d, teams: d.teams.map((x) => (x.team_id === tg.id ? { ...x, ...v } : x)) }));
    else
      patch((d) => {
        const existing = d.users.find((x) => x.user_id === tg.id);
        if (existing) return { ...d, users: d.users.map((x) => (x.user_id === tg.id ? { ...x, ...v } : x)) };
        const picked = tg.newUser;
        return picked
          ? {
              ...d,
              users: [
                ...d.users,
                {
                  user_id: picked.id,
                  email: picked.email,
                  display_name: picked.display_name ?? null,
                  team_name: picked.team_name ?? null,
                  used: 0,
                  ...v,
                },
              ],
            }
          : d;
      });
  };
  const onCleared = (tg: QuotaEditTarget) => {
    if (tg.scope === 'org') patch((d) => ({ ...d, org: null }));
    else if (tg.scope === 'teams')
      patch((d) => ({
        ...d,
        teams: d.teams.map((x) => (x.team_id === tg.id ? { ...x, monthly_query_limit: null, per_request_cap: null } : x)),
      }));
    else patch((d) => ({ ...d, users: d.users.filter((x) => x.user_id !== tg.id) }));
  };

  return (
    <section id="web-search-quota" className="space-y-4" data-testid="web-search-quota">
      <div>
        <h2 className="text-lg font-semibold">{t('sectionTitle')}</h2>
        <p className="mt-1 text-sm text-muted-foreground">{t('sectionDesc')}</p>
      </div>

      {/* 1) 조직 요약 */}
      <div className="glass rounded-apple p-5" data-testid="ws-quota-org">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="grid flex-1 grid-cols-1 gap-4 sm:grid-cols-3">
            <div>
              <p className="text-xs text-muted-foreground">{t('orgUsedLabel')}</p>
              <p className="mt-1 text-3xl font-bold tabular-nums" data-testid="ws-quota-org-used">
                {data.org_used.toLocaleString('en-US')}
              </p>
              <p className="text-xs text-muted-foreground">{t('orgUsedCaption')}</p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">{t('perUserLimit')}</p>
              <p className="mt-1 text-lg font-semibold" data-testid="ws-quota-org-limit">
                <LimitText value={orgLimit} inherited={t('unlimited')} />
              </p>
            </div>
            <div>
              <p className="text-xs text-muted-foreground">{t('defaultCapLabel')}</p>
              <p className="mt-1 text-lg font-semibold" data-testid="ws-quota-org-cap">
                <LimitText value={orgCap} inherited={t('defaultCap', { cap: gatewayCap })} />
              </p>
            </div>
          </div>
          <button
            type="button"
            className={ACTION_BTN}
            onClick={() =>
              setEditing({
                scope: 'org',
                id: null,
                name: t('orgTitle'),
                values: data.org,
                monthlyLabel: t('perUserLimit'),
                monthlyInherited: t('unlimited'),
                capInherited: String(gatewayCap),
                removable: data.org != null,
              })
            }
          >
            {t('setLimit')}
          </button>
        </div>
      </div>

      {/* 2) 팀 */}
      <div className="space-y-2">
        <h3 className="text-sm font-semibold">{t('teamsTitle')}</h3>
        <div className="w-full glass rounded-apple overflow-hidden">
          <Table data-testid="ws-quota-teams">
            <THead>
              <Tr>
                <Th>{t('colTarget')}</Th>
                <Th numeric>{t('colSearches')}</Th>
                <HeadWithHint label={t('colTeamLimit')} hint={t('colTeamLimitHint')} numeric />
                <Th className="min-w-[120px]">{t('colUsage')}</Th>
                <HeadWithHint label={t('perRequestCap')} hint={t('colCapHint')} numeric />
                <Th>{t('colActions')}</Th>
              </Tr>
            </THead>
            <TBody>
              {teams.length === 0 ? (
                <TEmpty colSpan={6}>{t('teamsEmpty')}</TEmpty>
              ) : (
                teams.map((tm) => {
                  const name = teamLabel(tm);
                  const has = tm.monthly_query_limit != null || tm.per_request_cap != null;
                  return (
                    <Tr key={tm.team_id} data-team-id={tm.team_id}>
                      <Td emphasis>
                        <span className="font-medium">{name}</span>
                      </Td>
                      <Td numeric>{tm.used.toLocaleString('en-US')}</Td>
                      <Td numeric>
                        <LimitText value={tm.monthly_query_limit} inherited={t('unlimited')} />
                      </Td>
                      <Td>
                        <UsageCell used={tm.used} limit={tm.monthly_query_limit} />
                      </Td>
                      <Td numeric>
                        <LimitText value={tm.per_request_cap} inherited={inheritedCapText} />
                      </Td>
                      <Td>
                        <button
                          type="button"
                          className={ACTION_BTN}
                          aria-label={t('setLimitFor', { target: name })}
                          onClick={() =>
                            setEditing({
                              scope: 'teams',
                              id: tm.team_id,
                              name,
                              values: has ? tm : null,
                              monthlyLabel: t('colTeamLimit'),
                              monthlyInherited: t('unlimited'),
                              capInherited: String(orgCap ?? gatewayCap),
                              removable: has,
                            })
                          }
                        >
                          {t('setLimit')}
                        </button>
                      </Td>
                    </Tr>
                  );
                })
              )}
            </TBody>
          </Table>
        </div>
      </div>

      {/* 3) 사용자 개별 지정 */}
      <div className="space-y-2">
        <div className="flex flex-wrap items-end justify-between gap-2">
          <h3 className="text-sm font-semibold">{t('usersTitle')}</h3>
          <AddUserOverride
            onPick={(u) => {
              const existing = data.users.find((x) => x.user_id === u.id);
              setEditing({
                newUser: existing ? undefined : u,
                scope: 'users',
                id: u.id,
                name: u.display_name ?? u.email,
                values: existing ?? null,
                monthlyLabel: t('colUserLimit'),
                monthlyInherited: orgLimitText,
                capInherited: String(orgCap ?? gatewayCap),
                removable: !!existing,
              });
            }}
          />
        </div>
        <div className="w-full glass rounded-apple overflow-hidden">
          <Table data-testid="ws-quota-users">
            <THead>
              <Tr>
                <Th>{t('colTarget')}</Th>
                <Th numeric>{t('colSearches')}</Th>
                <HeadWithHint label={t('colUserLimit')} hint={t('colUserLimitHint')} numeric />
                <Th className="min-w-[120px]">{t('colUsage')}</Th>
                <HeadWithHint label={t('perRequestCap')} hint={t('colCapHint')} numeric />
                <Th>{t('colActions')}</Th>
              </Tr>
            </THead>
            <TBody>
              {data.users.length === 0 ? (
                <TEmpty colSpan={6}>{t('usersEmpty')}</TEmpty>
              ) : (
                data.users.map((u) => {
                  const name = u.display_name ?? u.email;
                  const sub = [u.display_name ? u.email : null, u.team_name].filter(Boolean).join(' · ');
                  const effLimit = u.monthly_query_limit ?? orgLimit;
                  return (
                    <Tr key={u.user_id} data-user-id={u.user_id}>
                      <Td emphasis className="max-w-[18rem]">
                        <div className="truncate font-medium">{name}</div>
                        {sub && <div className="truncate text-xs text-muted-foreground">{sub}</div>}
                      </Td>
                      <Td numeric>{u.used.toLocaleString('en-US')}</Td>
                      <Td numeric>
                        <LimitText value={u.monthly_query_limit} inherited={orgLimitText} />
                      </Td>
                      <Td>
                        <UsageCell used={u.used} limit={effLimit} />
                      </Td>
                      <Td numeric>
                        <LimitText value={u.per_request_cap} inherited={inheritedCapText} />
                      </Td>
                      <Td>
                        <button
                          type="button"
                          className={ACTION_BTN}
                          aria-label={t('setLimitFor', { target: name })}
                          onClick={() =>
                            setEditing({
                              scope: 'users',
                              id: u.user_id,
                              name,
                              values: u,
                              monthlyLabel: t('colUserLimit'),
                              monthlyInherited: orgLimitText,
                              capInherited: String(orgCap ?? gatewayCap),
                              removable: true,
                            })
                          }
                        >
                          {t('setLimit')}
                        </button>
                      </Td>
                    </Tr>
                  );
                })
              )}
            </TBody>
          </Table>
        </div>
      </div>

      <QuotaDialog target={editing} onClose={() => setEditing(null)} onSaved={onSaved} onCleared={onCleared} />
    </section>
  );
}
