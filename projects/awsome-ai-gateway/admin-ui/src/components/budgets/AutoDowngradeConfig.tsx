'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useTransition, useEffect } from 'react';
import { useTranslations } from 'next-intl';
import { TrendingDown, ArrowRight, Plus, X } from 'lucide-react';
import { useToast } from '@/components/common/ToastProvider';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { DowngradeDiagram } from '@/components/common/DowngradeDiagram';
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
  /** 페이지에서 /admin/models 조회가 실패했을 때 true — 규칙 select 가
   *  "활성 모델 없음"으로 오독되지 않도록 로드 실패 경고를 표시한다. */
  modelsLoadFailed?: boolean;
  /** 팀 월간 예산 현재 사용률(%) — 시뮬레이터 초기값 + "저장 즉시 적용" 경고용. */
  currentUsagePct?: number | null;
}

export function AutoDowngradeConfig({ scopeType, scopeId, scopeName, models, modelsLoadFailed, currentUsagePct }: AutoDowngradeConfigProps) {
  const t = useTranslations('budgets');
  const tc = useTranslations('common');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [enabled, setEnabled] = useState(false);
  const [rules, setRules] = useState<DowngradeRuleForm[]>([]);
  // 마지막으로 저장된 스냅샷 — 이 값과 같은 규칙 행은 "저장됨" 초록 테두리로 표시.
  // 편집하면 스냅샷과 어긋나 테두리가 풀리고, 새 규칙은 처음부터 테두리가 없다.
  const [savedRules, setSavedRules] = useState<DowngradeRuleForm[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  // 사용률 시뮬레이터 — null 이면 꺼짐(기본: 현재 사용률 또는 0).
  const [simPct, setSimPct] = useState<number | null>(null);

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

  // 표시 순서는 소스 output 단가 내림차순 — 규칙 테이블이 사다리처럼 위→아래로
  // 읽힌다. 규칙 의미는 순서와 무관하다(소스당 규칙 하나, DB 유니크 제약).
  // from 미선택 행은 맨 아래로.
  const rowOrder = rules
    .map((_, i) => i)
    .sort(
      (a, b) =>
        (priceOf(rules[b].from_model_alias) ?? -1) -
        (priceOf(rules[a].from_model_alias) ?? -1),
    );

  // 같은 임계치로 이어진 체인 — 사용률 도달 순간 한 요청에서 최하위 모델까지
  // 다단 강등된다(apply_chain). 단계적 사다리를 의도했다면 임계치를 다르게.
  const hasFlattenedChain = rules.some(
    r1 =>
      r1.from_model_alias &&
      r1.to_model_alias &&
      rules.some(
        r2 =>
          r2 !== r1 &&
          r2.from_model_alias === r1.to_model_alias &&
          (parseInt(r2.threshold_pct) || 0) === (parseInt(r1.threshold_pct) || 0),
      ),
  );

  // 현재 사용률이 어떤 규칙의 임계치를 이미 넘었으면 저장 즉시 강등이 발동한다.
  const liveApplies =
    enabled &&
    currentUsagePct != null &&
    rules.some(
      r =>
        r.from_model_alias &&
        r.to_model_alias &&
        currentUsagePct >= (parseInt(r.threshold_pct) || 0),
    );

  // 시뮬레이터 표시값 — 슬라이더 미조작 시 현재 사용률(없으면 0).
  const effectiveSimPct = simPct ?? Math.round(currentUsagePct ?? 0);

  useEffect(() => {
    if (!scopeId) return;
    startTransition(async () => {
      const result = await getDowngradeConfigAction(scopeType, scopeId);
      if (result.success) {
        setEnabled(result.data.enabled);
        const loaded = result.data.rules.map(r => ({
          from_model_alias: r.from_model_alias,
          to_model_alias: r.to_model_alias,
          threshold_pct: String(r.threshold_pct),
        }));
        setRules(loaded);
        setSavedRules(loaded);
      }
      setLoaded(true);
    });
  }, [scopeType, scopeId]);

  const addRule = () => {
    // 빈 규칙 — from/to 는 placeholder 를 보여주고 사용자가 고른다.
    setRules(prev => [
      ...prev,
      { from_model_alias: '', to_model_alias: '', threshold_pct: '80' },
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
    // enabled=false(끄기 저장)는 규칙 검증을 건너뛴다 — 반쯤 편집된 규칙이
    // 있어도 끄기는 항상 가능해야 한다. 규칙은 비워 보낸다.
    if (enabled && rules.length === 0) {
      toast({ type: 'error', message: t('minOneRule'), auto_dismiss_ms: 3000 });
      return;
    }
    for (const rule of enabled ? rules : []) {
      if (!rule.from_model_alias) {
        toast({ type: 'error', message: t('selectFromModel'), auto_dismiss_ms: 3000 });
        return;
      }
      if (!rule.to_model_alias) {
        toast({ type: 'error', message: t('selectToModel'), auto_dismiss_ms: 3000 });
        return;
      }
      if (rule.from_model_alias === rule.to_model_alias) {
        toast({ type: 'error', message: t('sameSourceTarget', { alias: rule.from_model_alias }), auto_dismiss_ms: 3000 });
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
        rules: enabled
          ? rules.map(r => ({ ...r, threshold_pct: parseInt(r.threshold_pct) || 0 }))
          : [],
      });
      if (result.success) {
        // 끄기 저장은 규칙을 삭제하지 않고 비활성화만 한다 — 규칙은 저장된
        // 설정으로 남으므로 스냅샷도 현재 규칙으로 갱신한다.
        setSavedRules(rules.map(r => ({ ...r })));
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
        setSavedRules([]);
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
          <p className="text-[11px] leading-relaxed text-muted-foreground">
            {t('thresholdHint')}
          </p>
          {modelsLoadFailed && (
            <p className="text-[11px] font-medium text-amber-600 dark:text-amber-400">
              {t('modelsLoadFailed')}
            </p>
          )}
          {rules.length === 0 ? (
            <div className="rounded-xl border border-dashed border-border px-4 py-5 text-center text-xs text-muted-foreground">
              {t('noRules')}
            </div>
          ) : (
            <div className="space-y-2">
              {rowOrder.map((index, position) => {
                const rule = rules[index];
                return (
                <div
                  key={index}
                  className={`group flex items-center gap-2 rounded-xl border px-3 py-2 ${
                    savedRules.some(
                      s =>
                        s.from_model_alias === rule.from_model_alias &&
                        s.to_model_alias === rule.to_model_alias &&
                        s.threshold_pct === rule.threshold_pct,
                    )
                      ? 'border-emerald-500/60 bg-emerald-500/5'
                      : 'border-border/60 bg-background/60'
                  }`}
                >
                  <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-muted text-[10px] font-semibold tabular-nums text-muted-foreground">
                    {position + 1}
                  </span>
                  <select
                    value={rule.from_model_alias}
                    onChange={e => updateRule(index, 'from_model_alias', e.target.value)}
                    className="min-w-0 flex-1 rounded-lg border border-transparent bg-transparent px-2 py-1.5 font-mono text-xs hover:bg-muted/50 focus:border-input focus:bg-background focus:outline-none focus:ring-1 focus:ring-ring"
                  >
                    {!rule.from_model_alias && (
                      <option value="" disabled>
                        {t('selectFromModel')}
                      </option>
                    )}
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
                    {!rule.to_model_alias && (
                      <option value="" disabled>
                        {rule.from_model_alias && cheaperModels(rule.from_model_alias).length === 0
                          ? t('noCheaperModel', { alias: rule.from_model_alias })
                          : t('selectToModel')}
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
                  <div
                    className="flex shrink-0 items-center gap-1 rounded-lg border border-input bg-background px-2 py-1 focus-within:ring-1 focus-within:ring-ring"
                    title={t('thresholdInputTitle')}
                  >
                    <input
                      type="number"
                      min={1}
                      max={100}
                      value={rule.threshold_pct}
                      onChange={e => updateRule(index, 'threshold_pct', e.target.value)}
                      className="w-12 bg-transparent text-center text-xs tabular-nums focus:outline-none"
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
              );})}
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

          {hasFlattenedChain && (
            <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-700 dark:text-amber-300">
              {t('sameThresholdWarning')}
            </p>
          )}

          {liveApplies && (
            <p className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-[11px] leading-relaxed text-amber-700 dark:text-amber-300">
              {t('liveApplyWarning', { pct: Math.round(currentUsagePct ?? 0) })}
            </p>
          )}

          {rules.length > 0 && (
            <div className="space-y-2">
              <div className="flex items-center gap-3">
                <span className="shrink-0 text-[11px] font-medium text-muted-foreground">
                  {t('simulateTitle')}
                </span>
                <input
                  type="range"
                  min={0}
                  max={100}
                  value={effectiveSimPct}
                  onChange={e => setSimPct(Number(e.target.value))}
                  className="h-1.5 flex-1 cursor-pointer accent-primary"
                  aria-label={t('simulateTitle')}
                />
                <span className="w-10 shrink-0 text-right text-[11px] font-semibold tabular-nums">
                  {effectiveSimPct}%
                </span>
                {currentUsagePct != null && (
                  <button
                    type="button"
                    onClick={() => setSimPct(Math.round(currentUsagePct))}
                    className="shrink-0 rounded-md border border-border px-1.5 py-0.5 text-[10px] text-muted-foreground hover:bg-muted"
                    title={t('simulateCurrentHint')}
                  >
                    {t('simulateCurrent')}
                  </button>
                )}
              </div>
              <DowngradeDiagram
                rules={rules}
                models={activeModels}
                formatOutPrice={formatOutPrice}
                simPct={effectiveSimPct}
                simLabels={{
                  noChange: src => t('simNoChange', { src }),
                  resolved: (src, dst, hops) =>
                    t('simResolved', { src, dst, hops }),
                }}
                terminalLabel={t('terminalTag')}
              />
            </div>
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
            onClick={() => setClearOpen(true)}
            disabled={isPending}
            className="text-sm text-destructive hover:underline disabled:opacity-50"
          >
            {t('clearConfig')}
          </button>
        )}
      </div>

      <ConfirmDialog
        isOpen={clearOpen}
        onClose={() => setClearOpen(false)}
        onConfirm={handleDisable}
        title={t('clearConfigTitle')}
        message={t('clearConfigConfirm', { scope: scopeName })}
        confirmLabel={t('clearConfig')}
        isDestructive
      />
    </div>
  );
}
