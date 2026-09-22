'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { TrendingDown, ArrowRight, Plus, X } from 'lucide-react';
import { useToast } from '@/components/common/ToastProvider';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import {
  getDowngradeConfigAction,
  setDowngradeConfigAction,
  deleteDowngradeConfigAction,
} from '@/lib/actions/budgets';
import type { ModelListItem } from '@/types/entities';

interface DowngradeRuleForm {
  from_model_alias: string;
  to_model_alias: string;
  threshold_pct: string;
}

interface AutoDowngradeConfigProps {
  scopeType: 'TEAM' | 'USER';
  scopeId: string;
  scopeName: string;
  models: ModelListItem[];
}

export function AutoDowngradeConfig({ scopeType, scopeId, scopeName, models }: AutoDowngradeConfigProps) {
  const t = useTranslations('budgets');
  const tc = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [enabled, setEnabled] = useState(false);
  const [rules, setRules] = useState<DowngradeRuleForm[]>([]);
  const [loaded, setLoaded] = useState(false);

  const activeModels = models.filter(m => m.is_active);

  const priceOf = (alias: string) =>
    activeModels.find(m => m.alias === alias)?.output_price_per_1k;

  // 다운그레이드는 output 단가가 낮은 모델로만 — from 보다 비싼 모델은 후보에서 제외.
  const cheaperModels = (fromAlias: string): ModelListItem[] => {
    const p = priceOf(fromAlias);
    if (p == null) return activeModels;
    return activeModels.filter(m => m.output_price_per_1k < p);
  };

  // 저장값은 per-1K — 표기는 1M 기준 output 단가가 읽기 쉽다 ($0.015/1K → $15/1M).
  const formatOutPrice = (m: ModelListItem) => {
    const per1m = m.output_price_per_1k * 1000;
    return `$${per1m.toFixed(2).replace(/\.?0+$/, '')}/1M output`;
  };

  useEffect(() => {
    if (!scopeId) return;
    startTransition(async () => {
      const result = await getDowngradeConfigAction(scopeType, scopeId);
      if (result.success) {
        setEnabled(result.data.enabled);
        setRules(
          result.data.rules.map(r => ({
            from_model_alias: r.from_model_alias,
            to_model_alias: r.to_model_alias,
            threshold_pct: String(r.threshold_pct),
          })),
        );
      }
      setLoaded(true);
    });
  }, [scopeType, scopeId]);

  const addRule = () => {
    // 기본 from 은 output 단가가 가장 높은 모델 — 저렴한 to 후보가 존재하도록.
    const from = [...activeModels].sort(
      (a, b) => b.output_price_per_1k - a.output_price_per_1k,
    )[0]?.alias ?? '';
    setRules(prev => [
      ...prev,
      {
        from_model_alias: from,
        to_model_alias: cheaperModels(from)[0]?.alias ?? '',
        threshold_pct: '80',
      },
    ]);
  };

  const removeRule = (index: number) => {
    setRules(prev => prev.filter((_, i) => i !== index));
  };

  const updateRule = (index: number, field: keyof DowngradeRuleForm, value: string | number) => {
    setRules(prev =>
      prev.map((r, i) => {
        if (i !== index) return r;
        const next = { ...r, [field]: value };
        // from 변경 시 기존 to 가 더 이상 저렴하지 않으면 첫 번째 유효 후보로 교체.
        if (field === 'from_model_alias' && !cheaperModels(next.from_model_alias).some(m => m.alias === next.to_model_alias)) {
          next.to_model_alias = cheaperModels(next.from_model_alias)[0]?.alias ?? '';
        }
        return next;
      }),
    );
  };

  const handleSave = () => {
    if (rules.length === 0) {
      toast({ type: 'error', message: t('minOneRule'), auto_dismiss_ms: 3000 });
      return;
    }
    for (const rule of rules) {
      if (rule.from_model_alias === rule.to_model_alias) {
        toast({ type: 'error', message: t('sameSourceTarget', { alias: rule.from_model_alias }), auto_dismiss_ms: 3000 });
        return;
      }
      if (!rule.to_model_alias) {
        toast({ type: 'error', message: t('noCheaperModel', { alias: rule.from_model_alias }), auto_dismiss_ms: 4000 });
        return;
      }
      if (!cheaperModels(rule.from_model_alias).some(m => m.alias === rule.to_model_alias)) {
        toast({ type: 'error', message: t('targetNotCheaper', { from: rule.from_model_alias, to: rule.to_model_alias }), auto_dismiss_ms: 4000 });
        return;
      }
    }
    startTransition(async () => {
      const result = await setDowngradeConfigAction(scopeType, scopeId, {
        enabled,
        rules: rules.map(r => ({ ...r, threshold_pct: parseInt(r.threshold_pct) || 0 })),
      });
      if (result.success) {
        toast({ type: 'success', message: t('downgradeSaved'), auto_dismiss_ms: 3000 });
      } else {
        let msg = result.error;
        if (msg?.includes('Budget must be configured') || msg?.includes('must be greater than 0 for downgrade')) {
          msg = t('noBudgetForTeam');
        }
        toast({ type: 'error', message: msg, auto_dismiss_ms: 5000 });
      }
    });
  };

  const handleDisable = () => {
    startTransition(async () => {
      const result = await deleteDowngradeConfigAction(scopeType, scopeId);
      if (result.success) {
        setEnabled(false);
        setRules([]);
        toast({ type: 'success', message: t('downgradeCleared'), auto_dismiss_ms: 3000 });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 5000 });
      }
    });
  };

  if (!loaded) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
        <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
        {t('configLoading')}
      </div>
    );
  }

  return (
    <div className="space-y-4 rounded-apple border border-border/60 bg-muted/20 p-5">
      <div className="flex items-start justify-between gap-4">
        <div className="flex items-center gap-3 min-w-0">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-primary/10 text-primary">
            <TrendingDown size={17} />
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h3 className="text-sm font-semibold truncate">{t('autoDowngrade')}</h3>
              <span
                className={`inline-flex items-center rounded-full px-2 py-0.5 text-[10px] font-semibold ${
                  enabled
                    ? 'bg-emerald-500/15 text-emerald-600 dark:text-emerald-400'
                    : 'bg-muted text-muted-foreground'
                }`}
              >
                {enabled ? tc('enabled') : tc('disabled')}
              </span>
            </div>
            <p className="mt-0.5 text-xs text-muted-foreground truncate">
              {t('autoDowngradeDesc', { scope: scopeName })}
            </p>
          </div>
        </div>
        <label className="flex shrink-0 items-center gap-2 cursor-pointer pt-1">
          <div className="relative inline-flex items-center">
            <input
              type="checkbox"
              checked={enabled}
              onChange={e => setEnabled(e.target.checked)}
              className="sr-only peer"
            />
            <div className="w-10 h-[22px] bg-muted-foreground/30 peer-checked:bg-primary rounded-full transition-colors after:content-[''] after:absolute after:top-[3px] after:left-[3px] after:w-4 after:h-4 after:bg-background after:rounded-full after:shadow-sm after:transition-transform peer-checked:after:translate-x-[18px]" />
          </div>
        </label>
      </div>

      {enabled && (
        <div className="space-y-3">
          {rules.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-4 py-5 text-center text-xs text-muted-foreground">
              {t('noRules')}
            </div>
          ) : (
            <div className="space-y-2">
              {rules.map((rule, index) => (
                <div
                  key={index}
                  className="group flex items-center gap-2 rounded-xl border border-border/60 bg-background/60 px-3 py-2"
                >
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-semibold tabular-nums text-muted-foreground">
                    {index + 1}
                  </span>
                  <select
                    value={rule.from_model_alias}
                    onChange={e => updateRule(index, 'from_model_alias', e.target.value)}
                    className="min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1.5 font-mono text-xs hover:bg-muted/50 focus:border-input focus:bg-background focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    {activeModels.map(m => (
                      <option key={m.alias} value={m.alias}>
                        {m.alias} ({formatOutPrice(m)})
                      </option>
                    ))}
                  </select>
                  <ArrowRight size={14} className="shrink-0 text-muted-foreground" />
                  <select
                    value={rule.to_model_alias}
                    onChange={e => updateRule(index, 'to_model_alias', e.target.value)}
                    className="min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1.5 font-mono text-xs hover:bg-muted/50 focus:border-input focus:bg-background focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    {cheaperModels(rule.from_model_alias).length === 0 && (
                      <option value="" disabled>
                        {t('noCheaperModel', { alias: rule.from_model_alias })}
                      </option>
                    )}
                    {/* 기존 저장분이 필터에 걸리는 경우 현재 값을 유지해 보여준다
                        (저장 시 검증에서 걸러짐) */}
                    {rule.to_model_alias &&
                      !cheaperModels(rule.from_model_alias).some(m => m.alias === rule.to_model_alias) && (
                        <option value={rule.to_model_alias}>
                          {rule.to_model_alias} — {tc('inactive')}
                        </option>
                      )}
                    {cheaperModels(rule.from_model_alias).map(m => (
                      <option key={m.alias} value={m.alias}>
                        {m.alias} ({formatOutPrice(m)})
                      </option>
                    ))}
                  </select>
                  <div className="flex shrink-0 items-center gap-1 rounded-lg border border-input bg-background px-2 py-1 focus-within:ring-1 focus-within:ring-ring">
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={rule.threshold_pct}
                      onChange={e => updateRule(index, 'threshold_pct', e.target.value)}
                      className="w-10 bg-transparent text-center text-xs tabular-nums focus:outline-none"
                    />
                    <span className="text-[10px] text-muted-foreground">%</span>
                  </div>
                  <button
                    type="button"
                    onClick={() => removeRule(index)}
                    className="shrink-0 rounded-md p-1 text-muted-foreground opacity-0 transition-opacity hover:bg-destructive/10 hover:text-destructive focus-visible:opacity-100 group-hover:opacity-100"
                    title={tc('delete')}
                    aria-label={tc('delete')}
                  >
                    <X size={14} />
                  </button>
                </div>
              ))}
            </div>
          )}

          <button
            type="button"
            onClick={addRule}
            className="flex w-full items-center justify-center gap-1.5 rounded-xl border border-dashed border-border px-3 py-2 text-xs font-medium text-muted-foreground transition-colors hover:border-primary/50 hover:bg-primary/5 hover:text-primary"
          >
            <Plus size={13} />
            {t('addRule')}
          </button>

          {rules.length > 0 && (
            <DowngradeDiagram
              rules={rules}
              models={activeModels}
              formatOutPrice={formatOutPrice}
            />
          )}
        </div>
      )}

      <div className="flex items-center gap-3 border-t border-border/60 pt-3">
        <SpinnerButton
          onClick={handleSave}
          isLoading={isPending}
          disabled={!enabled && rules.length === 0}
          className="rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50"
        >
          {t('saveConfig')}
        </SpinnerButton>
        {enabled && (
          <button
            type="button"
            onClick={handleDisable}
            disabled={isPending}
            className="text-sm text-destructive hover:underline disabled:opacity-50"
          >
            {t('clearConfig')}
          </button>
        )}
      </div>
    </div>
  );
}
// ─── DowngradeDiagram ────────────────────────────────────────────────────────
// 규칙(from→to)을 DAG로 시각화 — 노드=모델(output 단가 표기), 엣지=규칙(% 라벨).
// 노드 깊이는 relaxation으로 계산: 어떤 규칙의 to 인 노드는 max(from 깊이)+1.

function DowngradeDiagram({
  rules,
  models,
  formatOutPrice,
}: {
  rules: DowngradeRuleForm[];
  models: ModelListItem[];
  formatOutPrice: (_m: ModelListItem) => string;
}) {
  const depth = new Map<string, number>();
  for (const r of rules) {
    if (!depth.has(r.from_model_alias)) depth.set(r.from_model_alias, 0);
  }
  for (let pass = 0; pass < rules.length; pass++) {
    for (const r of rules) {
      const d = (depth.get(r.from_model_alias) ?? 0) + 1;
      if ((depth.get(r.to_model_alias) ?? -1) < d) depth.set(r.to_model_alias, d);
    }
  }

  const maxDepth = Math.max(0, ...depth.values());
  const layerCount = maxDepth + 1;
  const priceOf = (alias: string) =>
    models.find(m => m.alias === alias)?.output_price_per_1k ?? 0;

  const layers: string[][] = Array.from({ length: layerCount }, () => []);
  for (const [alias, d] of depth) layers[d].push(alias);
  for (const l of layers) l.sort((a, b) => priceOf(b) - priceOf(a));

  const pos = new Map<string, { l: number; i: number; n: number }>();
  layers.forEach((nodes, l) =>
    nodes.forEach((a, i) => pos.set(a, { l, i, n: nodes.length })),
  );

  // 칩은 컬럼 폭의 86% (중앙 정렬) — 엣지는 칩의 좌/우 끝에 닿도록 보정.
  const chipPad = (100 / layerCount) * 0.07;
  const edgeX1 = (l: number) => ((l + 1) / layerCount) * 100 - chipPad;
  const edgeX2 = (l: number) => (l / layerCount) * 100 + chipPad;
  const nodeY = (i: number, n: number) => ((i + 0.5) / n) * 100;
  const height = Math.max(1, ...layers.map(l => l.length)) * 52;

  return (
    <div className="rounded-xl border border-border/60 bg-muted/20 px-3 py-3">
      <div className="relative" style={{ height }}>
        {layers.map((nodes, l) => (
          <div
            key={l}
            className="absolute top-0 bottom-0 flex flex-col justify-around"
            style={{ left: `${(l / layerCount) * 100}%`, width: `${100 / layerCount}%` }}
          >
            {nodes.map(alias => {
              const m = models.find(mm => mm.alias === alias);
              return (
                <div
                  key={alias}
                  className="mx-auto w-[86%] rounded-lg border border-border/60 bg-background/90 px-2 py-1 text-center shadow-sm"
                >
                  <div className="truncate font-mono text-[11px]">{alias}</div>
                  <div className="text-[9px] tabular-nums text-muted-foreground">
                    {m ? formatOutPrice(m) : '—'}
                  </div>
                </div>
              );
            })}
          </div>
        ))}

        <svg
          className="absolute inset-0 h-full w-full"
          viewBox="0 0 100 100"
          preserveAspectRatio="none"
          aria-hidden="true"
        >
          <defs>
            <marker
              id="dg-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M0,0 L10,5 L0,10 z" className="fill-muted-foreground" />
            </marker>
          </defs>
          {rules.map((r, idx) => {
            const a = pos.get(r.from_model_alias);
            const b = pos.get(r.to_model_alias);
            if (!a || !b) return null;
            const x1 = edgeX1(a.l);
            const y1 = nodeY(a.i, a.n);
            const x2 = edgeX2(b.l);
            const y2 = nodeY(b.i, b.n);
            const dx = Math.max((x2 - x1) * 0.5, 4);
            return (
              <path
                key={idx}
                d={`M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`}
                fill="none"
                strokeWidth="1.5"
                vectorEffect="non-scaling-stroke"
                className="stroke-muted-foreground/60"
                markerEnd="url(#dg-arrow)"
              />
            );
          })}
        </svg>

        {rules.map((r, idx) => {
          const a = pos.get(r.from_model_alias);
          const b = pos.get(r.to_model_alias);
          if (!a || !b) return null;
          const mx = (edgeX1(a.l) + edgeX2(b.l)) / 2;
          const my = (nodeY(a.i, a.n) + nodeY(b.i, b.n)) / 2;
          return (
            <span
              key={`t${idx}`}
              className="absolute -translate-x-1/2 -translate-y-1/2 rounded-full border border-border/60 bg-background px-1.5 py-px text-[9px] font-semibold tabular-nums text-muted-foreground"
              style={{ left: `${mx}%`, top: `${my}%` }}
            >
              {r.threshold_pct}%
            </span>
          );
        })}
      </div>
    </div>
  );
}
