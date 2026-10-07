'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// /users 패널 임베드용 rate-limit 편집 — /rate-limits 페이지의 RateLimitConfigPanel
// 을 대체한다. 다른 정책 패널(ScopeAppAccessPanel)과 같은 규칙:
//   · 상속 중이면 필드는 비우고 상속값은 placeholder 로만 표시 —
//     value 에 넣으면 무심코 저장할 때 상속값이 개별 override 로 굳는다.
//   · 저장은 부모의 통합 Apply 가 ref.save() 로 호출(hideActions 임베드 전용).
//   · "상위 정책 따라가기"는 staged — pendingDelete 플래그를 켜고 Apply 시 DELETE.
//   · 실시간 사용량 폴링·트렌드 차트는 섹션이 열려 있을 때만 동작한다
//     (PolicySectionOpenContext — 접힌 섹션의 폴링은 서버 비용만 쓴다).

import { forwardRef, useContext, useEffect, useImperativeHandle, useRef, useState, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import {
  deleteRateLimitAction,
  getRateLimitStatusAction,
  setRateLimitAction,
  type RateLimitScopeStatusData,
} from '@/lib/actions/rate-limits';
import { fetchRateLimitUsage, type RateLimitUsage } from '@/lib/utils/rateLimitUsage';
import { UsageTrendChart } from '@/components/rate-limits/UsageTrendChart';
import { PolicySectionOpenContext } from '@/components/common/PolicySection';
import { UnsavedApplyBar } from '@/components/common/UnsavedApplyBar';
import { FormError } from '@/components/common/FormError';
import { useToast } from '@/components/common/ToastProvider';
import type { PolicySummary } from '@/components/common/policySummary';

export interface ScopeRateLimitHandle {
  save: () => Promise<boolean>;
  revert: () => void;
}

interface ScopeRateLimitPanelProps {
  scope: 'user' | 'team';
  scopeId: string;
  /** user 스코프의 상속 출처 표시(팀 이름). 없으면 범용 문구. */
  inheritedFromLabel?: string;
  hideActions?: boolean;
  bare?: boolean;
  /** 부모 저장 진행 중 — 편집을 잠근다. */
  disabled?: boolean;
  onDirtyChange?: (_dirty: boolean) => void;
  onSummaryChange?: (_summary: PolicySummary) => void;
}

interface Fields {
  rpm: string;
  tpm: string;
  cpm: string;
  cph: string;
}

const EMPTY_FIELDS: Fields = { rpm: '', tpm: '', cpm: '', cph: '' };

function fieldsFromConfig(
  cfg: RateLimitScopeStatusData['own']
): Fields {
  if (!cfg) return EMPTY_FIELDS;
  return {
    rpm: cfg.rpm != null ? String(cfg.rpm) : '',
    tpm: cfg.tpm != null ? String(cfg.tpm) : '',
    cpm: cfg.cpm != null ? String(cfg.cpm) : '',
    cph: cfg.cph != null ? String(cfg.cph) : '',
  };
}

function fieldsKey(f: Fields): string {
  return `${f.rpm}|${f.tpm}|${f.cpm}|${f.cph}`;
}

function parsePosInt(val: string): number | null {
  const n = parseInt(val, 10);
  return isNaN(n) || n <= 0 ? null : n;
}

function parsePosFloat(val: string): number | null {
  const n = parseFloat(val);
  return isNaN(n) || n < 0 ? null : n;
}

export const ScopeRateLimitPanel = forwardRef<ScopeRateLimitHandle, ScopeRateLimitPanelProps>(
  function ScopeRateLimitPanel(
    { scope, scopeId, inheritedFromLabel, hideActions, bare, disabled, onDirtyChange, onSummaryChange },
    ref
  ) {
    const t = useTranslations('users.rateLimit');
    const { toast } = useToast();
    const sectionOpen = useContext(PolicySectionOpenContext);
    const [isLoadPending, startLoadTransition] = useTransition();
    const [isSavePending, startSaveTransition] = useTransition();

    const [status, setStatus] = useState<RateLimitScopeStatusData | null>(null);
    const [loaded, setLoaded] = useState(false);
    const [fields, setFields] = useState<Fields>(EMPTY_FIELDS);
    // 저장된 own 값이 기준선 — 상속값은 placeholder 라 baseline 에 들어가지 않는다.
    const [baseline, setBaseline] = useState<Fields>(EMPTY_FIELDS);
    // "상위 정책 따라가기"는 staged — Apply 시 DELETE. 되돌리기로 취소 가능.
    const [pendingDelete, setPendingDelete] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [usage, setUsage] = useState<RateLimitUsage | null>(null);
    const mountedRef = useRef(true);
    useEffect(() => () => {
      mountedRef.current = false;
    }, []);

    useEffect(() => {
      let cancelled = false;
      setLoaded(false);
      setStatus(null);
      setPendingDelete(false);
      setError(null);
      startLoadTransition(async () => {
        const r = await getRateLimitStatusAction(scope, scopeId);
        if (cancelled) return;
        if (!r.success) {
          toast({ type: 'error', message: t('loadError'), auto_dismiss_ms: 5000 });
          return;
        }
        setStatus(r.data);
        const own = fieldsFromConfig(r.data.own);
        setFields(own);
        setBaseline(own);
        setLoaded(true);
      });
      return () => {
        cancelled = true;
      };
    }, [scope, scopeId, t, toast]);

    // 실시간 RPM 사용량 — 섹션이 열려 있을 때만 폴링한다. deps 는 scopeId
    // (객체 identity 가 아닌 값)라 재조회로 interval 이 재시작되지 않는다.
    useEffect(() => {
      if (!sectionOpen) return;
      let alive = true;
      const load = () =>
        fetchRateLimitUsage(scope.toUpperCase(), scopeId)
          .then((u) => {
            if (alive) setUsage(u);
          })
          .catch(() => {
            if (alive) setUsage(null);
          });
      load();
      const interval = setInterval(load, 10_000);
      return () => {
        alive = false;
        clearInterval(interval);
      };
    }, [sectionOpen, scope, scopeId]);

    const busy = isLoadPending || isSavePending || disabled === true;
    const dirty =
      loaded && (fieldsKey(fields) !== fieldsKey(baseline) || pendingDelete);

    useEffect(() => {
      onDirtyChange?.(dirty);
    }, [dirty, onDirtyChange]);

    // 헤더 배지 — 저장된 유효 상태(own ?? inherited). 편집 중 값은 dirty 마커가 담당.
    useEffect(() => {
      const effective = status ? (status.own ?? status.inherited) : null;
      const count = effective
        ? [effective.rpm, effective.tpm, effective.cpm, effective.cph].filter(
            (v) => v != null
          ).length
        : 0;
      onSummaryChange?.({
        loaded,
        restricted: count > 0,
        count,
        source: status?.own ? 'own' : status?.inherited ? 'team' : 'none',
      });
    }, [status, loaded, onSummaryChange]);

    const reload = async (): Promise<RateLimitScopeStatusData | null> => {
      const r = await getRateLimitStatusAction(scope, scopeId);
      if (!mountedRef.current) return null;
      if (!r.success) return null;
      setStatus(r.data);
      const own = fieldsFromConfig(r.data.own);
      setFields(own);
      setBaseline(own);
      return r.data;
    };

    const save = async (): Promise<boolean> => {
      if (!loaded) {
        toast({ type: 'error', message: t('loadError'), auto_dismiss_ms: 4000 });
        return false;
      }
      setError(null);
      if (pendingDelete) {
        const r = await deleteRateLimitAction(scope, scopeId);
        if (!mountedRef.current) return r.success;
        if (!r.success) {
          toast({ type: 'error', message: r.error, auto_dismiss_ms: 4000 });
          return false;
        }
        setPendingDelete(false);
        await reload();
        if (!hideActions) {
          toast({ type: 'success', message: t('saveSuccess'), auto_dismiss_ms: 4000 });
        }
        return true;
      }
      if (fieldsKey(fields) === fieldsKey(baseline)) return true;
      const r = await setRateLimitAction({
        target_id: scopeId,
        scope: scope.toUpperCase(),
        rpm: parsePosInt(fields.rpm),
        tpm: parsePosInt(fields.tpm),
        cpm: parsePosFloat(fields.cpm),
        cph: parsePosFloat(fields.cph),
      });
      if (!mountedRef.current) return r.success;
      if (!r.success) {
        setError(r.error);
        return false;
      }
      await reload();
      if (!hideActions) {
        toast({ type: 'success', message: t('saveSuccess'), auto_dismiss_ms: 4000 });
      }
      return true;
    };

    const revert = () => {
      setFields(baseline);
      setPendingDelete(false);
      setError(null);
    };

    useImperativeHandle(ref, () => ({ save, revert }));

    const effective = status ? (status.own ?? status.inherited) : null;
    const inherited = status && status.own == null ? status.inherited : null;
    const hasOwn = status?.own != null;

    const inputCls =
      'w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50 disabled:cursor-not-allowed';
    const placeholderFor = (key: keyof Fields) =>
      inherited && inherited[key] != null ? String(inherited[key]) : t('unlimited');

    const fieldRow = (
      key: keyof Fields,
      label: string,
      float = false
    ) => (
      <div className="space-y-1" key={key}>
        <label className="text-sm font-medium">{label}</label>
        <input
          type="number"
          min={float ? 0 : 1}
          step={float ? '0.0001' : undefined}
          value={fields[key]}
          onChange={(e) => setFields((prev) => ({ ...prev, [key]: e.target.value }))}
          placeholder={placeholderFor(key)}
          disabled={busy || !loaded || pendingDelete}
          className={inputCls}
        />
        <p className="text-xs text-muted-foreground">{t('emptyMeansUnlimited')}</p>
      </div>
    );

    return (
      <div className={bare ? '' : 'border rounded-apple-md p-3'}>
        {/* 상속/개별설정 출처 캡션 */}
        {loaded && inherited && (
          <p className="text-xs text-muted-foreground mb-3">
            {t('inheritCaption', {
              source:
                inheritedFromLabel ??
                (scope === 'user' ? t('inheritTeamFallback') : t('inheritGlobalFallback')),
            })}
          </p>
        )}
        {loaded && hasOwn && (
          <div className="flex items-center justify-between gap-2 mb-3">
            <p className="text-xs text-muted-foreground">{t('customCaption')}</p>
            <button
              type="button"
              disabled={busy || pendingDelete}
              onClick={() => setPendingDelete(true)}
              className="pressable flex-shrink-0 rounded-apple-sm border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary disabled:opacity-50"
            >
              {scope === 'user' ? t('followTeam') : t('clearLimit')}
            </button>
          </div>
        )}
        {pendingDelete && (
          <p className="text-xs text-amber-600 mb-3">{t('pendingDeleteHint')}</p>
        )}

        {isLoadPending ? (
          <div className="text-xs text-muted-foreground py-1">{t('loading')}</div>
        ) : !loaded ? (
          <div className="text-xs text-destructive py-1">{t('loadError')}</div>
        ) : (
          <>
            {/* 실시간 사용량 — 섹션이 열려 있을 때만 폴링됐다.
                tracked=false(RPM 한도 미설정)면 proxy 가 카운터를 적재하지 않으므로
                0 을 "요청 없음"으로 오독하지 않게 — 표시로 구분한다. */}
            {usage?.available && (
              <div className="mb-4 rounded-md border border-border bg-card/50 px-3 py-2.5">
                <div className="flex items-center justify-between">
                  <span className="text-xs font-medium text-muted-foreground">
                    {t('liveRpm', { seconds: usage.window_sec })}
                  </span>
                  {usage.tracked === false ? (
                    <span className="text-sm font-semibold text-muted-foreground">—</span>
                  ) : (
                    <span className="num text-sm font-semibold text-foreground">
                      {usage.rpm_used_total}
                      {effective?.rpm != null && (
                        <span className="text-muted-foreground font-normal"> / {effective.rpm}</span>
                      )}
                    </span>
                  )}
                </div>
                {usage.tracked !== false && effective?.rpm != null && effective.rpm > 0 && (
                  <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
                    <div
                      className="h-full rounded-full bg-primary transition-all"
                      style={{
                        width: `${Math.min(100, (usage.rpm_used_total / effective.rpm) * 100)}%`,
                      }}
                    />
                  </div>
                )}
                {usage.by_model.length > 0 && (
                  <ul className="mt-2 space-y-0.5 text-[11px] text-muted-foreground">
                    {usage.by_model.slice(0, 5).map((m) => (
                      <li key={m.model_alias} className="flex justify-between">
                        <span className="font-mono">{m.model_alias}</span>
                        <span className="num">{m.rpm_used}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {usage.tracked === false ? (
                  <p className="mt-1 text-[11px] text-muted-foreground">
                    {scope === 'user' && inherited
                      ? t('untrackedHintInherited')
                      : t('untrackedHint')}
                  </p>
                ) : (
                  usage.rpm_used_total === 0 && (
                    <p className="mt-1 text-[11px] text-muted-foreground">
                      {t('noRecentRequests', { seconds: usage.window_sec })}
                    </p>
                  )
                )}
              </div>
            )}

            {sectionOpen && (
              <UsageTrendChart
                scope={scope.toUpperCase()}
                scopeId={scopeId}
                limits={{
                  rpm: effective?.rpm,
                  tpm: effective?.tpm,
                  cph: effective?.cph,
                }}
              />
            )}

            <div className="space-y-3">
              {fieldRow('rpm', 'RPM (Requests Per Minute)')}
              {fieldRow('tpm', 'TPM (Tokens Per Minute)')}
              {fieldRow('cpm', 'CPM (Cost Per Minute, USD)', true)}
              {fieldRow('cph', 'CPH (Cost Per Hour, USD)', true)}
            </div>

            <FormError error={error} />
          </>
        )}

        {!hideActions && dirty && (
          <UnsavedApplyBar
            isPending={isSavePending}
            disabled={busy}
            onApply={() => startSaveTransition(async () => { await save(); })}
          />
        )}
      </div>
    );
  }
);
