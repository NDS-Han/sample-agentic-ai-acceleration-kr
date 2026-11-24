'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Fragment, useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { cn } from '@/lib/utils/cn';
import { fmtLatencyMs, fmtUsd, usdTitle } from '@/lib/utils/format';
import { modelDisplay } from '@/lib/utils/modelLabel';
import { userLabel } from '@/lib/userLabel';
import { ErrorState } from '@/components/common/ErrorState';
import {
  MIX_BUCKETS,
  cellTooltip,
  userSubline,
  cellValue,
  fmtInt,
  fmtTok,
  heatBackground,
  heatIntensity,
  tokenMix,
  type CellTipLabels,
  type SortColumn,
  type SortState,
  type TokenMode,
  type UserModelCell,
  type UserModelDetail,
  type UserModelResponse,
  type UserModelRow,
} from './userModel';

/** 행의 표시 이름 — 이름 없으면 이메일(합성 이메일은 userLabel 규칙). */
export function rowName(r: { name?: string | null; email?: string | null }): string {
  return r.name?.trim() || userLabel({ email: r.email ?? '' }) || '—';
}

function useTipLabels(): CellTipLabels {
  const t = useTranslations('analytics.userModel');
  return {
    input: t('tipInput'),
    output: t('tipOutput'),
    cacheRead: t('tipCacheRead'),
    cacheWrite: t('tipCacheWrite'),
    total: t('tipTotal'),
    requests: (count) => t('tipRequests', { count }),
  };
}

/** 한 셀 — 미사용(키 없음)은 '—', 사용은 모드 값 + sqrt 히트 + 분해 툴팁. */
export function HeatCell({
  cell,
  mode,
  max,
  model,
  user,
}: {
  cell: UserModelCell | null | undefined;
  mode: TokenMode;
  max: number;
  model: string;
  user: string;
}) {
  const t = useTranslations('analytics.userModel');
  const labels = useTipLabels();
  if (!cell) {
    return (
      <td
        className="px-1 py-1 text-right text-sm text-muted-foreground/70"
        title={t('notUsed', { model })}
        data-unused="true"
      >
        <span className="block px-2.5 py-1.5">—</span>
      </td>
    );
  }
  const v = cellValue(cell, mode);
  return (
    <td className="px-1 py-1 text-right" title={cellTooltip(`${model} · ${user}`, cell, labels)}>
      <span
        className="block rounded-md px-2.5 py-1.5 text-sm font-medium text-foreground num"
        style={{ background: heatBackground(heatIntensity(v, max)) }}
      >
        {fmtTok(v)}
      </span>
    </td>
  );
}

/** 토큰 구성 막대 — 입력/출력/캐시 읽기/쓰기 비율. */
export function TokenMixBar({ cell, className }: { cell: UserModelCell; className?: string }) {
  const t = useTranslations('analytics.userModel');
  const pct = tokenMix(cell);
  return (
    <div
      role="img"
      aria-label={t('mixAria', {
        input: fmtTok(cell.input_tokens),
        output: fmtTok(cell.output_tokens),
        cacheRead: fmtTok(cell.cache_read_tokens),
        cacheWrite: fmtTok(cell.cache_write_tokens),
      })}
      className={cn('flex h-2 overflow-hidden rounded bg-muted', className)}
    >
      {MIX_BUCKETS.map((b, i) =>
        pct[i] > 0 ? (
          <span key={b.key} style={{ width: `${pct[i]}%`, background: b.color }} />
        ) : null,
      )}
    </div>
  );
}

export function MixLegend() {
  const t = useTranslations('analytics.userModel');
  const names = [t('colInput'), t('colOutput'), t('colCacheRead'), t('colCacheWrite')];
  return (
    <span className="flex flex-wrap gap-3 text-xs text-muted-foreground">
      {MIX_BUCKETS.map((b, i) => (
        <span key={b.key} className="inline-flex items-center gap-1.5">
          <span className="inline-block h-2 w-2 rounded-sm" style={{ background: b.color }} aria-hidden="true" />
          {names[i]}
        </span>
      ))}
    </span>
  );
}

type DetailState =
  | { status: 'loading' }
  | { status: 'error' }
  | { status: 'ok'; data: UserModelDetail };

/** 상세 조회 상태 훅 — 펼친 행·드로어가 같이 쓴다(조회는 섹션의 캐시를 거친다). */
export function useUserModelDetail(
  userId: string | null,
  fetchDetail: (_userId: string) => Promise<UserModelDetail>,
): { state: DetailState; retry: () => void } {
  const [state, setState] = useState<DetailState>({ status: 'loading' });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    if (!userId) return;
    let alive = true;
    setState({ status: 'loading' });
    fetchDetail(userId).then(
      (data) => alive && setState({ status: 'ok', data }),
      () => alive && setState({ status: 'error' }),
    );
    return () => {
      alive = false;
    };
  }, [userId, fetchDetail, attempt]);
  return { state, retry: () => setAttempt((n) => n + 1) };
}

function DetailPanel({
  userId,
  name,
  fetchDetail,
}: {
  userId: string;
  name: string;
  fetchDetail: (_userId: string) => Promise<UserModelDetail>;
}) {
  const t = useTranslations('analytics.userModel');
  const { state, retry } = useUserModelDetail(userId, fetchDetail);
  if (state.status === 'loading') {
    return <div className="h-20 animate-pulse rounded-md bg-muted/60" aria-busy="true" />;
  }
  if (state.status === 'error') {
    return <ErrorState compact message={t('detailFailed')} onRetry={retry} />;
  }
  const models = state.data.models;
  const right = 'px-2 py-1.5 text-right num';
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <span className="text-sm font-semibold">
          {t('detailTitle', { name, count: models.length })}
        </span>
        <MixLegend />
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-xs">
          <thead>
            <tr className="text-muted-foreground">
              <th scope="col" className="px-2 py-1.5 text-left font-medium">{t('colModel')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colRequests')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colInput')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colOutput')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colCacheRead')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colCacheWrite')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colTotal')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colCost')}</th>
              <th scope="col" className={cn(right, 'font-medium')}>{t('colAvgLatency')}</th>
              <th scope="col" className="px-2 py-1.5 text-left font-medium">{t('colMix')}</th>
            </tr>
          </thead>
          <tbody>
            {models.map((m) => (
              <tr key={m.model} className="border-t border-[--table-divider]">
                <td className="px-2 py-1.5 font-medium" title={m.model}>
                  {modelDisplay(m.model, m.display_name)}
                </td>
                <td className={right}>{fmtInt(m.requests)}</td>
                <td className={right}>{fmtTok(m.input_tokens)}</td>
                <td className={right}>{fmtTok(m.output_tokens)}</td>
                <td className={right}>{fmtTok(m.cache_read_tokens)}</td>
                <td className={right}>{fmtTok(m.cache_write_tokens)}</td>
                <td className={cn(right, 'font-semibold')}>{fmtTok(m.total_tokens)}</td>
                <td className={right} title={usdTitle(Number(m.cost_usd))}>
                  {fmtUsd(Number(m.cost_usd) || 0)}
                </td>
                <td className={right}>{fmtLatencyMs(m.avg_latency_ms)}</td>
                <td className="px-2 py-1.5">
                  <TokenMixBar cell={m} className="w-28" />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

interface UserModelTableProps {
  data: UserModelResponse;
  mode: TokenMode;
  sort: SortState;
  onSort: (_col: SortColumn) => void;
  expanded: ReadonlySet<string>;
  onToggle: (_userId: string) => void;
  fetchDetail: (_userId: string) => Promise<UserModelDetail>;
  /** "표에서 보기" 직후 그 행의 펼침 버튼으로 포커스를 옮긴다. */
  focusUserId?: string | null;
  onFocused?: () => void;
  emptyText: string;
}

export function UserModelTable({
  data,
  mode,
  sort,
  onSort,
  expanded,
  onToggle,
  fetchDetail,
  focusUserId,
  onFocused,
  emptyText,
}: UserModelTableProps) {
  const t = useTranslations('analytics.userModel');
  const labels = useTipLabels();
  const max = Number(data.max_cell?.[mode]) || 0;
  // Other 에 든 모델이 없으면 'Other (0)' 열은 통째로 숨긴다(헤더·셀·colSpan).
  const showOther = data.other_models.length > 0;
  const colCount = 2 + data.top_models.length + (showOther ? 1 : 0) + 2;

  useEffect(() => {
    if (!focusUserId) return;
    const el = document.querySelector<HTMLElement>(`[data-expand-user="${focusUserId.replace(/["\\]/g, "")}"]`);
    if (el) {
      el.focus();
      onFocused?.();
    }
  }, [focusUserId, data, onFocused]);

  const otherNames = data.other_models.map((m) => modelDisplay(m.model, m.display_name)).join(', ');
  const head = (col: SortColumn, label: string, opts: { left?: boolean; tip?: string } = {}) => {
    const on = sort.col === col;
    return (
      <th
        key={col}
        scope="col"
        aria-sort={on ? (sort.dir === 'desc' ? 'descending' : 'ascending') : 'none'}
        className={cn('h-10 px-1 align-middle', opts.left ? 'text-left' : 'text-right')}
      >
        <button
          type="button"
          onClick={() => onSort(col)}
          title={opts.tip ?? t('sortBy', { column: label })}
          className={cn(
            'max-w-[11rem] truncate rounded px-2 py-1 text-xs whitespace-nowrap transition-colors',
            'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
            on ? 'font-semibold text-primary' : 'font-medium text-muted-foreground hover:text-foreground',
          )}
        >
          {label}
          {on && <span aria-hidden="true">{sort.dir === 'desc' ? ' ↓' : ' ↑'}</span>}
        </button>
      </th>
    );
  };

  return (
    <div className="relative w-full overflow-x-auto">
      <table className="w-full min-w-[960px] text-sm" aria-label={t('tableLabel')}>
        <thead className="[&_tr]:border-b [&_tr]:border-border">
          <tr>
            <th scope="col" className="w-9">
              <span className="sr-only">{t('colExpand')}</span>
            </th>
            {head('name', t('colUser'), { left: true })}
            {data.top_models.map((m) => {
              const label = modelDisplay(m.model, m.display_name);
              return head(`model:${m.model}`, label, { tip: `${t('sortBy', { column: label })}\n${m.model}` });
            })}
            {showOther &&
              head('other', t('colOther', { count: data.other_models.length }), {
                tip: t('otherModels', { models: otherNames }),
              })}
            {head('total', t('colTotal'))}
            {head('cost', t('colCost'))}
          </tr>
        </thead>
        <tbody>
          {data.rows.length === 0 ? (
            <tr>
              <td colSpan={colCount} className="px-3 py-12 text-center text-sm text-muted-foreground">
                {emptyText}
              </td>
            </tr>
          ) : (
            data.rows.map((r: UserModelRow) => {
              const name = rowName(r);
              const open = expanded.has(r.user_id);
              const sub = userSubline(r);
              return (
                <Fragment key={r.user_id}>
                  <tr className={cn('border-b border-[--table-divider]', open ? 'bg-muted/40' : 'hover:bg-[--table-row-hover]')}>
                    <td className="px-1 py-1 align-middle">
                      <button
                        type="button"
                        data-expand-user={r.user_id}
                        onClick={() => onToggle(r.user_id)}
                        aria-expanded={open}
                        aria-label={open ? t('collapse', { name }) : t('expand', { name })}
                        className="flex h-7 w-7 items-center justify-center rounded-md border border-border text-muted-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                      >
                        {open ? <ChevronDown size={14} aria-hidden="true" /> : <ChevronRight size={14} aria-hidden="true" />}
                      </button>
                    </td>
                    <td className="max-w-[16rem] px-3 py-1.5">
                      <div className="truncate font-medium" title={r.email ?? undefined}>{name}</div>
                      {sub && <div className="truncate text-xs text-muted-foreground">{sub}</div>}
                    </td>
                    {data.top_models.map((m) => (
                      <HeatCell
                        key={m.model}
                        cell={r.cells?.[m.model]}
                        mode={mode}
                        max={max}
                        model={modelDisplay(m.model, m.display_name)}
                        user={name}
                      />
                    ))}
                    {showOther && (
                      <HeatCell cell={r.other} mode={mode} max={max} model={t('colOther', { count: data.other_models.length })} user={name} />
                    )}
                    <td
                      className="px-3 py-1.5 text-right font-bold num"
                      title={cellTooltip(`${t('colTotal')} · ${name}`, r.total, labels)}
                    >
                      {fmtTok(cellValue(r.total, mode))}
                    </td>
                    <td className="px-3 py-1.5 text-right num" title={usdTitle(Number(r.total.cost_usd))}>
                      {fmtUsd(Number(r.total.cost_usd) || 0)}
                    </td>
                  </tr>
                  {open && (
                    <tr className="border-b border-[--table-divider]">
                      <td colSpan={colCount} className="px-3 pb-3 pt-1">
                        <div className="ml-8 rounded-apple-sm border border-border bg-muted/30 p-3">
                          <DetailPanel userId={r.user_id} name={name} fetchDetail={fetchDetail} />
                        </div>
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
