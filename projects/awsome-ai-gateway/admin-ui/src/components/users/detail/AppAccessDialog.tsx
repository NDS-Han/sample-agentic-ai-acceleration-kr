'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 앱 접근 변경 — 세 가지 중 하나:
 *   - 상위 정책 따르기(사용자: 팀, 팀: 조직, 조직: 제한 없음) → 이 레벨의 정책 행 삭제
 *   - 모든 앱 허용(개별 지정)  → 세 앱을 모두 담은 명시 목록(상속을 끊는다)
 *   - 선택한 앱만 허용         → 고른 앱만
 * 저장은 기존 API 그대로: 사용자 PUT/DELETE /admin/users/{id}/allowed-clients,
 * 팀·조직 PUT/DELETE /admin/{teams|organizations}/{id}/allowed-clients.
 */

import { useEffect, useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';

import {
  getScopeAllowedClientsAction,
  getUserAllowedClientsAction,
  setScopeAllowedClientsAction,
  setUserAllowedClientsAction,
} from '@/lib/actions/users';
import { CLIENTS } from '@/lib/constants/gateway';
import { labelFor } from '@/lib/utils/modelLabel';
import { EditPolicyDialog } from './EditPolicyDialog';

export type AppScope = 'user' | 'team' | 'organization';
export type AppMode = 'inherit' | 'all' | 'select';

/** 저장된 목록 → 대화상자 모드. [] = 정책 없음(상속). */
export function modeOf(own: string[]): { mode: AppMode; selected: string[] } {
  if (own.length === 0) return { mode: 'inherit', selected: [] };
  if (CLIENTS.every((c) => own.includes(c))) return { mode: 'all', selected: [...CLIENTS] };
  return { mode: 'select', selected: CLIENTS.filter((c) => own.includes(c)) };
}

/** 모드 → 저장할 목록. */
export function clientsFor(mode: AppMode, selected: string[]): string[] {
  if (mode === 'inherit') return [];
  if (mode === 'all') return [...CLIENTS];
  return CLIENTS.filter((c) => selected.includes(c));
}

export function AppAccessDialog({
  isOpen,
  onClose,
  scope,
  id,
  target,
  inheritedText,
  onSaved,
}: {
  isOpen: boolean;
  onClose: () => void;
  scope: AppScope;
  id: string;
  target: string;
  /** 상위 정책 현재 값 문구(예: "제한 없음 · 3개 앱 모두"). */
  inheritedText: string;
  onSaved: () => void;
}) {
  const t = useTranslations('usersDetail.apps');
  const [state, setState] = useState<'loading' | 'failed' | 'ready'>('loading');
  const [own, setOwn] = useState<string[]>([]);
  const [mode, setMode] = useState<AppMode>('inherit');
  const [selected, setSelected] = useState<string[]>([]);

  useEffect(() => {
    if (!isOpen) return;
    let alive = true;
    setState('loading');
    const load = scope === 'user' ? getUserAllowedClientsAction(id) : getScopeAllowedClientsAction(scope, id);
    load.then((r) => {
      if (!alive) return;
      if (!r.success) {
        setState('failed');
        return;
      }
      const m = modeOf(r.data.clients);
      setOwn(r.data.clients);
      setMode(m.mode);
      setSelected(m.selected);
      setState('ready');
    });
    return () => {
      alive = false;
    };
  }, [isOpen, scope, id]);

  const next = clientsFor(mode, selected);
  const before = clientsFor(modeOf(own).mode, modeOf(own).selected);
  const dirty = state === 'ready' && next.join(',') !== before.join(',');
  const invalid = mode === 'select' && selected.length === 0;
  const inheritLabel = scope === 'user' ? t('inheritTeam') : scope === 'team' ? t('inheritOrg') : t('inheritNone');

  const modes = useMemo(
    () =>
      [
        { value: 'inherit' as const, label: inheritLabel, desc: scope === 'organization' ? t('inheritNoneDesc') : t('inheritDesc', { value: inheritedText }) },
        { value: 'all' as const, label: t('all'), desc: t('allDesc') },
        { value: 'select' as const, label: t('select'), desc: t('selectDesc') },
      ],
    [inheritLabel, inheritedText, scope, t],
  );

  const save = async () => {
    const r =
      scope === 'user'
        ? await setUserAllowedClientsAction(id, next)
        : await setScopeAllowedClientsAction(scope, id, next);
    return r.success ? ({ ok: true } as const) : ({ ok: false, error: r.error } as const);
  };
  const revert = async () => {
    const r = scope === 'user' ? await setUserAllowedClientsAction(id, []) : await setScopeAllowedClientsAction(scope, id, []);
    return r.success ? ({ ok: true } as const) : ({ ok: false, error: r.error } as const);
  };

  return (
    <EditPolicyDialog
      isOpen={isOpen}
      onClose={onClose}
      title={t('title')}
      target={target}
      info={scope === 'user' ? t('infoUser') : scope === 'team' ? t('infoTeam') : t('infoOrg')}
      summary={
        !dirty
          ? t('noChange')
          : mode === 'inherit'
            ? t('summaryInherit')
            : t('summaryOwn', { apps: next.map((c) => labelFor(c, c)).join(' · ') })
      }
      dirty={dirty}
      invalid={invalid}
      loading={state === 'loading'}
      loadFailed={state === 'failed'}
      onSave={save}
      onRevert={revert}
      revertLabel={scope === 'organization' ? t('revertOrg') : undefined}
      revertDisabled={own.length === 0}
      onSaved={onSaved}
    >
      <div role="radiogroup" aria-label={t('title')} className="space-y-2">
        {modes.map((o) => (
          <label
            key={o.value}
            className={`flex cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 transition-colors ${
              mode === o.value ? 'border-primary bg-primary/5' : 'border-border hover:bg-muted/40'
            }`}
          >
            <input
              type="radio"
              name="app-mode"
              value={o.value}
              checked={mode === o.value}
              onChange={() => {
                setMode(o.value);
                if (o.value === 'select' && selected.length === 0) setSelected(own.length ? modeOf(own).selected : []);
              }}
              className="mt-0.5 h-4 w-4 accent-[hsl(var(--primary))]"
            />
            <span className="min-w-0">
              <span className="block text-sm font-medium">{o.label}</span>
              <span className="block text-xs text-muted-foreground">{o.desc}</span>
            </span>
          </label>
        ))}
      </div>
      {mode === 'select' && (
        <fieldset className="space-y-2">
          <legend className="sr-only">{t('select')}</legend>
          <div className="flex flex-wrap gap-2">
            {CLIENTS.map((c) => (
              <label
                key={c}
                className={`flex cursor-pointer items-center gap-2 rounded-md border px-3 py-1.5 text-sm ${
                  selected.includes(c) ? 'border-primary' : 'border-border'
                }`}
              >
                <input
                  type="checkbox"
                  checked={selected.includes(c)}
                  onChange={() =>
                    setSelected((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]))
                  }
                  className="h-4 w-4 accent-[hsl(var(--primary))]"
                />
                {labelFor(c, c)}
              </label>
            ))}
          </div>
          {invalid && (
            <p role="alert" className="text-xs text-destructive">
              {t('pickOne')}
            </p>
          )}
        </fieldset>
      )}
    </EditPolicyDialog>
  );
}
