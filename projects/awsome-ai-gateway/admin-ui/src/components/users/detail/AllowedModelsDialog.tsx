'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 허용 모델 변경 — 계열별 묶음(Haiku·Sonnet·Opus·GPT·오픈 모델), 검색, 전체/허용/차단 필터,
 * 그룹 전체 선택, 표시명 + 별칭 id, '이름 중복'·'변경' 배지.
 *
 * 저장(기존 API 그대로):
 *   - 사용자: PUT /admin/users/{id}/allowed-models (전체 목록) · 되돌리기 = DELETE(팀 정책 상속)
 *     ⚠️ 상속 중에 하나라도 바꿔 저장하면 **목록 전체**가 이 사용자의 개별 정책이 된다
 *        (이후 팀 정책 변경이 따라오지 않는다) — 대화상자가 경고한다.
 *   - 팀: PUT /admin/teams/{id}/allowed-models — 전체 선택은 [] (= 제한 없음)로 저장한다.
 */

import { useEffect, useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Search } from 'lucide-react';

import { getUserAllowedModelsAction, setUserAllowedModelsAction } from '@/lib/actions/users';
import { getTeamAllowedModelsAction, setTeamAllowedModelsAction } from '@/lib/actions/models';
import type { ModelListItem } from '@/types/entities';
import { EditPolicyDialog } from './EditPolicyDialog';
import {
  changedCount,
  displayLabel,
  duplicateNames,
  groupModels,
  type ModelFilter,
} from './modelGroups';

export function AllowedModelsDialog({
  isOpen,
  onClose,
  scope,
  id,
  target,
  catalog,
  inherited,
  inheriting,
  onSaved,
}: {
  isOpen: boolean;
  onClose: () => void;
  scope: 'user' | 'team';
  id: string;
  target: string;
  /** 활성 모델 카탈로그(listActiveModelsAction). */
  catalog: ModelListItem[];
  /** 사용자: 지금 물려받는 목록(null = 제한 없음). 팀에는 쓰지 않는다. */
  inherited?: string[] | null;
  /** 사용자가 지금 팀 정책을 따르는 중인가 — 경고 표시. */
  inheriting?: boolean;
  onSaved: () => void;
}) {
  const t = useTranslations('usersDetail.models');
  const [state, setState] = useState<'loading' | 'failed' | 'ready'>('loading');
  const [own, setOwn] = useState<string[]>([]);
  const [base, setBase] = useState<Set<string>>(new Set());
  const [checked, setChecked] = useState<Set<string>>(new Set());
  const [query, setQuery] = useState('');
  const [filter, setFilter] = useState<ModelFilter>('all');
  const all = useMemo(() => catalog.map((m) => m.alias), [catalog]);

  useEffect(() => {
    if (!isOpen) return;
    let alive = true;
    setState('loading');
    setQuery('');
    setFilter('all');
    const load =
      scope === 'user'
        ? getUserAllowedModelsAction(id).then((r) => (r.success ? { ok: true as const, list: r.data.modelAliases } : { ok: false as const }))
        : getTeamAllowedModelsAction(id).then((r) => (r.success ? { ok: true as const, list: r.data.model_aliases } : { ok: false as const }));
    load.then((r) => {
      if (!alive) return;
      if (!r.ok) {
        setState('failed');
        return;
      }
      const start =
        r.list.length > 0
          ? r.list
          : scope === 'user' && inherited && inherited.length > 0
            ? inherited
            : all;
      const s = new Set(start.filter((a) => all.includes(a)));
      setOwn(r.list);
      setBase(s);
      setChecked(new Set(s));
      setState('ready');
    });
    return () => {
      alive = false;
    };
  }, [isOpen, scope, id, all, inherited]);

  const dupes = useMemo(() => duplicateNames(catalog), [catalog]);
  const groups = useMemo(() => groupModels(catalog, checked, query, filter), [catalog, checked, query, filter]);
  const changes = changedCount(base, checked);
  const dirty = state === 'ready' && changes > 0;
  const invalid = checked.size === 0;
  const allowedN = [...checked].filter((a) => all.includes(a)).length;

  const toggle = (alias: string) =>
    setChecked((prev) => {
      const n = new Set(prev);
      if (n.has(alias)) n.delete(alias);
      else n.add(alias);
      return n;
    });
  const setGroup = (aliases: string[], on: boolean) =>
    setChecked((prev) => {
      const n = new Set(prev);
      aliases.forEach((a) => (on ? n.add(a) : n.delete(a)));
      return n;
    });

  const save = async () => {
    const list = all.filter((a) => checked.has(a));
    const r =
      scope === 'user'
        ? await setUserAllowedModelsAction(id, list)
        : await setTeamAllowedModelsAction(id, list.length >= all.length ? [] : list);
    return r.success ? ({ ok: true } as const) : ({ ok: false, error: r.error } as const);
  };
  const revert = async () => {
    const r = scope === 'user' ? await setUserAllowedModelsAction(id, []) : await setTeamAllowedModelsAction(id, []);
    return r.success ? ({ ok: true } as const) : ({ ok: false, error: r.error } as const);
  };

  const filters: { value: ModelFilter; label: string }[] = [
    { value: 'all', label: t('filterAll', { count: all.length }) },
    { value: 'allowed', label: t('filterAllowed', { count: allowedN }) },
    { value: 'blocked', label: t('filterBlocked', { count: all.length - allowedN }) },
  ];

  return (
    <EditPolicyDialog
      isOpen={isOpen}
      onClose={onClose}
      title={t('title')}
      target={target}
      wide
      info={scope === 'user' ? (inheriting ? t('infoUserInheriting') : t('infoUserOwn')) : t('infoTeam')}
      warning={scope === 'user' && inheriting ? t('copyWarning') : undefined}
      summary={
        changes > 0
          ? t('summaryChanged', { allowed: allowedN, total: all.length, changed: changes })
          : t('summary', { allowed: allowedN, total: all.length })
      }
      dirty={dirty}
      invalid={invalid}
      loading={state === 'loading'}
      loadFailed={state === 'failed'}
      onSave={save}
      onRevert={revert}
      revertLabel={scope === 'team' ? t('revertTeam') : undefined}
      revertDisabled={own.length === 0}
      onSaved={onSaved}
    >
      <div className="flex flex-wrap items-center gap-2">
        <div className="relative min-w-[12rem] flex-1">
          <Search size={14} className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t('search')}
            aria-label={t('search')}
            className="w-full rounded-md border border-input bg-background py-1.5 pl-8 pr-3 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <div role="group" aria-label={t('filterLabel')} className="flex gap-1 rounded-md bg-muted p-1">
          {filters.map((f) => (
            <button
              key={f.value}
              type="button"
              aria-pressed={filter === f.value}
              onClick={() => setFilter(f.value)}
              className={`rounded px-2.5 py-1 text-xs font-medium focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring ${
                filter === f.value ? 'bg-background text-foreground shadow-sm' : 'text-muted-foreground hover:text-foreground'
              }`}
            >
              {f.label}
            </button>
          ))}
        </div>
      </div>
      <div className="max-h-[min(26rem,calc(100vh-22rem))] space-y-4 overflow-y-auto pr-1" data-testid="model-groups">
        {groups.length === 0 && <p className="py-6 text-center text-sm text-muted-foreground">{t('noMatch')}</p>}
        {groups.map((g) => {
          const on = g.all.filter((m) => checked.has(m.alias)).length;
          const allOn = on === g.all.length;
          return (
            <section key={g.family} aria-label={t(`family.${g.family}`)}>
              <div className="mb-1.5 flex items-center justify-between gap-2">
                <h4 className="text-xs font-semibold text-muted-foreground">
                  {t(`family.${g.family}`)} · {t('groupCount', { on, total: g.all.length })}
                </h4>
                <button
                  type="button"
                  onClick={() => setGroup(g.all.map((m) => m.alias), !allOn)}
                  className="rounded px-1.5 py-0.5 text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                >
                  {allOn ? t('groupNone') : t('groupAll')}
                </button>
              </div>
              <ul className="space-y-1">
                {g.items.map((m) => {
                  const label = displayLabel(m);
                  const isOn = checked.has(m.alias);
                  const changed = isOn !== base.has(m.alias);
                  return (
                    <li key={m.alias}>
                      <label
                        className={`flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2 ${
                          isOn ? 'border-primary/50' : 'border-border'
                        } hover:bg-muted/40`}
                      >
                        <input
                          type="checkbox"
                          checked={isOn}
                          aria-label={label === m.alias ? label : `${label} (${m.alias})`}
                          onChange={() => toggle(m.alias)}
                          className="h-4 w-4 shrink-0 accent-[hsl(var(--primary))]"
                        />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium">{label}</span>
                          <span className="block truncate font-mono text-[11px] text-muted-foreground">{m.alias}</span>
                        </span>
                        {dupes.has(label) && (
                          <span className="badge badge-amber whitespace-nowrap" title={t('duplicateHint')}>
                            {t('duplicate')}
                          </span>
                        )}
                        {changed && <span className="badge badge-sky whitespace-nowrap">{t('changed')}</span>}
                      </label>
                    </li>
                  );
                })}
              </ul>
            </section>
          );
        })}
      </div>
      {invalid && state === 'ready' && (
        <p role="alert" className="text-xs text-destructive">
          {scope === 'user' ? t('pickOneUser') : t('pickOneTeam')}
        </p>
      )}
    </EditPolicyDialog>
  );
}
