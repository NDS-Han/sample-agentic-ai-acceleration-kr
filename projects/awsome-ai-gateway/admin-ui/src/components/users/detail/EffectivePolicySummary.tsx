'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 실제 적용 결과(유효 정책) — 읽기 전용.
 *  - 요약 타일 4장: 사용 가능한 앱 · 모델(허용/차단) · 웹 검색 · Rate limit
 *  - 차단된 모델 칩 — 툴팁에 막은 정책(사용자 앱 정책 / 허용 모델 정책 / 모델의 앱 제한)
 *  - 자동 다운그레이드 경로 + 쓸 수 없는 단계 경고(기존 EffectivePolicyCard 와 같은 규칙)
 *  - 모델 × 앱 전체 표는 기본 접힘, 앱 열은 표시 이름(Claude Code · Cowork · Codex)
 */

import { useId, useState } from 'react';
import { useTranslations } from 'next-intl';
import { ChevronDown, ChevronRight } from 'lucide-react';

import { InfoTooltip } from '@/components/common/InfoTooltip';
import { CLIENTS } from '@/lib/constants/gateway';
import { labelFor, modelDisplay } from '@/lib/utils/modelLabel';
import type { EffectivePolicy, ModelListItem } from '@/types/entities';
import { blockedModels, rateSummary, unavailableChainModels } from './policyRows';
import { usePolicyText } from './usePolicyText';

const AXES = ['user_app', 'user_model', 'model_app'] as const;

export function EffectivePolicySummary({ policy, catalog }: { policy: EffectivePolicy; catalog: ModelListItem[] }) {
  const t = useTranslations('usersDetail.effective');
  const te = useTranslations('users.effectivePolicy');
  const text = usePolicyText(catalog);
  const [open, setOpen] = useState(false);
  const matrixId = useId();

  const names = new Map<string, string | null | undefined>();
  for (const m of policy.models ?? []) names.set(m.alias, m.display_name);
  for (const m of catalog) if (m.display_name) names.set(m.alias, m.display_name);
  const label = (alias: string) => modelDisplay(alias, names.get(alias) ?? null);

  const apps = policy.allowed_clients && policy.allowed_clients.length > 0 ? policy.allowed_clients : [...CLIENTS];
  const { allowed, blocked } = blockedModels(policy);
  const wsApps = Object.entries(policy.web_search).filter(([, on]) => on).map(([c]) => c);
  const cap = policy.web_search_quota?.per_request_cap;
  const rate = rateSummary(policy, 'user').effective;
  const unavailable = new Set(unavailableChainModels(policy));
  const rules = [...policy.downgrade_rules].sort((a, b) => a.threshold_pct - b.threshold_pct);
  const clients = [
    ...CLIENTS.filter((c) => policy.cells.some((x) => x.client === c)),
    ...[...new Set(policy.cells.map((x) => x.client))].filter((c) => !(CLIENTS as readonly string[]).includes(c)),
  ];
  const aliases = [...new Set(policy.cells.map((c) => c.model_alias))].sort((a, b) => label(a).localeCompare(label(b)));
  const reasonText = (r: string) => ((AXES as readonly string[]).includes(r) ? te(`axis.${r as (typeof AXES)[number]}`) : r);

  const tiles = [
    { key: 'apps', label: t('tileApps'), value: t('count', { count: apps.length }), sub: apps.map((c) => labelFor(c, c)).join(' · ') },
    {
      key: 'models',
      label: t('tileModels'),
      value: t('count', { count: allowed.length }),
      sub: blocked.length ? t('blockedCount', { count: blocked.length }) : t('noneBlocked'),
    },
    {
      key: 'ws',
      label: t('tileWebSearch'),
      value: wsApps.length ? t('wsOn') : t('wsOff'),
      sub: wsApps.length ? (cap != null ? t('wsCap', { cap }) : wsApps.map((c) => labelFor(c, c)).join(' · ')) : t('wsOffSub'),
    },
    { key: 'rate', label: t('tileRate'), value: text.rate(rate), sub: t('rateSub') },
  ];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="effective-tiles">
        {tiles.map((x) => (
          <div key={x.key} className="min-w-0 rounded-lg border border-border bg-muted/30 px-3 py-2.5" data-tile={x.key}>
            <p className="text-xs text-muted-foreground">{x.label}</p>
            <p className="truncate text-base font-semibold">{x.value}</p>
            <p className="truncate text-xs text-muted-foreground" title={x.sub}>{x.sub}</p>
          </div>
        ))}
      </div>

      {blocked.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-medium text-muted-foreground">{t('blockedTitle', { count: blocked.length })}</p>
          <ul className="flex flex-wrap gap-1.5" data-testid="blocked-chips">
            {blocked.map((b) => (
              <li key={b.alias}>
                <InfoTooltip
                  label={t('blockedChipLabel', { model: label(b.alias) })}
                  trigger={
                    <span className="inline-flex items-center gap-1 rounded-full border border-destructive/30 bg-destructive/10 px-2 py-0.5 text-xs font-medium text-destructive">
                      ✕ {label(b.alias)}
                    </span>
                  }
                >
                  <span className="block font-medium">{label(b.alias)}</span>
                  <span className="block font-mono text-[10px] text-muted-foreground">{b.alias}</span>
                  <span className="mt-1 block">{t('blockedBy', { reasons: b.reasons.map(reasonText).join(', ') || '—' })}</span>
                </InfoTooltip>
              </li>
            ))}
          </ul>
        </div>
      )}

      <div>
        <p className="mb-1.5 text-xs font-medium text-muted-foreground">{t('downgradeTitle')}</p>
        {rules.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t('downgradeNone')}</p>
        ) : (
          <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-sm" data-testid="downgrade-path">
            <span className={unavailable.has(rules[0].from_model_alias) ? 'text-destructive line-through' : 'font-medium'}>
              {label(rules[0].from_model_alias)}
            </span>
            {rules.map((r, i) => (
              <span key={i} className="inline-flex items-center gap-2">
                <span className="text-xs text-muted-foreground">
                  — {r.threshold_pct}% {r.scope === 'TEAM' ? te('scopeTeam') : te('scopeUser')} →
                </span>
                <span className={unavailable.has(r.to_model_alias) ? 'text-destructive line-through' : 'font-medium'}>
                  {label(r.to_model_alias)}
                </span>
              </span>
            ))}
          </p>
        )}
        {unavailable.size > 0 && (
          <p role="note" className="mt-1.5 text-xs text-amber-600 dark:text-amber-400">
            ⚠ {te('chainUnavailableHint', { models: [...unavailable].map(label).join(', ') })}
          </p>
        )}
      </div>

      <div>
        <button
          type="button"
          aria-expanded={open}
          aria-controls={matrixId}
          onClick={() => setOpen((v) => !v)}
          className="inline-flex items-center gap-1 rounded-md px-1 py-1 text-sm font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
        >
          {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
          {open ? t('matrixHide') : t('matrixShow', { count: aliases.length })}
        </button>
        {open && (
          <div id={matrixId} className="mt-2 overflow-x-auto">
            <table className="w-full min-w-[28rem] text-xs" data-testid="effective-matrix">
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="py-1.5 pr-3 text-left font-medium text-muted-foreground">{te('model')}</th>
                  {clients.map((c) => (
                    <th key={c} scope="col" className="px-3 py-1.5 text-center font-medium text-muted-foreground whitespace-nowrap">
                      {labelFor(c, c)}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {aliases.map((a) => (
                  <tr key={a} className="border-b border-border/60 last:border-0">
                    <td className="py-1.5 pr-3">
                      <span className="block font-medium">{label(a)}</span>
                      {label(a) !== a && <span className="block font-mono text-[10px] text-muted-foreground">{a}</span>}
                    </td>
                    {clients.map((c) => {
                      const cell = policy.cells.find((x) => x.client === c && x.model_alias === a);
                      if (!cell) return <td key={c} className="text-center text-muted-foreground">—</td>;
                      return cell.allowed ? (
                        <td key={c} className="text-center font-semibold text-teal-600 dark:text-teal-400" aria-label={te('allowed')}>
                          ✓
                        </td>
                      ) : (
                        <td
                          key={c}
                          className="text-center font-semibold text-destructive"
                          title={cell.blocked_by.map(reasonText).join(', ')}
                          aria-label={`${te('denied')}: ${cell.blocked_by.map(reasonText).join(', ')}`}
                        >
                          ✕
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
