'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Rate limit 변경 — RPM·TPM·CPM·CPH 네 줄, 단위 표시, 빈 칸 = 상속, 옆에 비웠을 때 적용되는 값
 * ("팀: 없음 → 조직: 없음 = 무제한"). 게이트웨이는 사용자·팀·조직 한도를 모두 검사한다.
 *
 * 저장: PUT /admin/rate-limits/{user|team}/{id} · 모두 비우고 저장 / 되돌리기 = DELETE.
 * API 는 양수만 받는다(0 으로 막는 표현은 없다).
 */

import { useEffect, useId, useState } from 'react';
import { useTranslations } from 'next-intl';

import { deleteRateLimitAction, getRateLimitStatusAction, setRateLimitAction } from '@/lib/actions/rate-limits';
import { fetchRateLimitTrend, trendPeaks, type TrendPeaks } from '@/lib/utils/rateLimitUsage';
import { EditPolicyDialog } from './EditPolicyDialog';
import { RATE_KEYS, type RateKey, type RateValues } from './policyRows';

type Fields = Record<RateKey, string>;
const EMPTY: Fields = { rpm: '', tpm: '', cpm: '', cph: '' };
const INT_KEYS: RateKey[] = ['rpm', 'tpm'];

/** '' → null(상속), 양수 → 숫자, 그 외 → undefined(오류). RPM·TPM 은 정수, CPM·CPH 는 소수 4자리까지. */
export function parseRate(key: RateKey, raw: string): number | null | undefined {
  const v = raw.trim();
  if (v === '') return null;
  const ok = INT_KEYS.includes(key) ? /^\d+$/.test(v) : /^\d+(\.\d{1,4})?$/.test(v);
  if (!ok) return undefined;
  const n = Number(v);
  return n > 0 ? n : undefined;
}

function toFields(v: RateValues | null): Fields {
  if (!v) return { ...EMPTY };
  return {
    rpm: v.rpm?.toString() ?? '',
    tpm: v.tpm?.toString() ?? '',
    cpm: v.cpm?.toString() ?? '',
    cph: v.cph?.toString() ?? '',
  };
}

/** 최근 24시간 최대 사용량 값 — RPM·TPM 은 올림한 정수, 비용은 $ 와 유효숫자 2개(1 이상은 소수 2자리). */
export function fmtPeak(key: RateKey, v: number): string {
  if (key === 'rpm' || key === 'tpm') return Math.ceil(v).toLocaleString('en-US');
  if (v === 0) return '$0';
  return v >= 1 ? `$${v.toFixed(2)}` : `$${Number(v.toPrecision(2))}`;
}

export function RateLimitDialog({
  isOpen,
  onClose,
  scope,
  id,
  target,
  team,
  org,
  onSaved,
}: {
  isOpen: boolean;
  onClose: () => void;
  scope: 'user' | 'team';
  id: string;
  target: string;
  /** 사용자 뷰 — 팀 설정(없으면 null). */
  team?: RateValues | null;
  /** 조직(GLOBAL) 설정. */
  org: RateValues | null;
  onSaved: () => void;
}) {
  const t = useTranslations('usersDetail.rate');
  const uid = useId();
  const [state, setState] = useState<'loading' | 'failed' | 'ready'>('loading');
  const [hasOwn, setHasOwn] = useState(false);
  const [base, setBase] = useState<Fields>({ ...EMPTY });
  const [fields, setFields] = useState<Fields>({ ...EMPTY });
  // 최근 24시간 실제 사용 최대치 — 한도를 정하는 동안 참고(읽기 전용). undefined = 조회 중·조회 불가.
  const [peaks, setPeaks] = useState<TrendPeaks | null | undefined>(undefined);

  useEffect(() => {
    if (!isOpen) return;
    let alive = true;
    setPeaks(undefined);
    fetchRateLimitTrend(scope.toUpperCase(), id, '24h').then(
      (tr) => {
        if (alive) setPeaks(tr.available ? trendPeaks(tr) : undefined);
      },
      () => {
        if (alive) setPeaks(undefined);
      },
    );
    return () => {
      alive = false;
    };
  }, [isOpen, scope, id]);

  useEffect(() => {
    if (!isOpen) return;
    let alive = true;
    setState('loading');
    getRateLimitStatusAction(scope, id).then((r) => {
      if (!alive) return;
      if (!r.success) {
        setState('failed');
        return;
      }
      const f = toFields(r.data.own);
      setHasOwn(r.data.own != null);
      setBase(f);
      setFields(f);
      setState('ready');
    });
    return () => {
      alive = false;
    };
  }, [isOpen, scope, id]);

  const parsed = Object.fromEntries(RATE_KEYS.map((k) => [k, parseRate(k, fields[k])])) as Record<RateKey, number | null | undefined>;
  const invalid = RATE_KEYS.some((k) => parsed[k] === undefined);
  const changed = RATE_KEYS.filter((k) => fields[k].trim() !== base[k].trim());
  const dirty = state === 'ready' && changed.length > 0;
  const allEmpty = RATE_KEYS.every((k) => parsed[k] == null);

  const fmt = (k: RateKey, v: number | null | undefined) =>
    v == null ? t('none') : k === 'cpm' || k === 'cph' ? `$${v}` : v.toLocaleString('en-US');
  const inheritedText = (k: RateKey) => {
    const orgV = org?.[k] ?? null;
    if (scope === 'user') {
      const teamV = team?.[k] ?? null;
      const eff = teamV ?? orgV;
      return t('chainUser', { team: fmt(k, teamV), org: fmt(k, orgV), eff: eff == null ? t('unlimited') : fmt(k, eff) });
    }
    return t('chainTeam', { org: fmt(k, orgV), eff: orgV == null ? t('unlimited') : fmt(k, orgV) });
  };

  const save = async () => {
    if (allEmpty) {
      const r = await deleteRateLimitAction(scope, id);
      return r.success ? ({ ok: true } as const) : ({ ok: false, error: r.error } as const);
    }
    const r = await setRateLimitAction({
      target_id: id,
      scope: scope.toUpperCase(),
      rpm: parsed.rpm ?? null,
      tpm: parsed.tpm ?? null,
      cpm: parsed.cpm ?? null,
      cph: parsed.cph ?? null,
    });
    return r.success ? ({ ok: true } as const) : ({ ok: false, error: r.error } as const);
  };
  const revert = async () => {
    const r = await deleteRateLimitAction(scope, id);
    return r.success ? ({ ok: true } as const) : ({ ok: false, error: r.error } as const);
  };

  return (
    <EditPolicyDialog
      isOpen={isOpen}
      onClose={onClose}
      title={t('title')}
      target={target}
      wide
      info={scope === 'user' ? t('infoUser') : t('infoTeam')}
      summary={
        !dirty
          ? t('noChange')
          : allEmpty
            ? scope === 'user'
              ? t('summaryInheritUser')
              : t('summaryInheritTeam')
            : t('summaryChanged', { count: changed.length })
      }
      dirty={dirty}
      invalid={invalid}
      loading={state === 'loading'}
      loadFailed={state === 'failed'}
      onSave={save}
      onRevert={revert}
      revertDisabled={!hasOwn}
      onSaved={onSaved}
    >
      <div className="overflow-x-auto">
        <table className="w-full min-w-[32rem] text-sm">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th scope="col" className="py-2 pr-3 font-medium">{t('colItem')}</th>
              <th scope="col" className="py-2 pr-3 font-medium">{scope === 'user' ? t('colUser') : t('colTeam')}</th>
              <th scope="col" className="py-2 font-medium">{t('colInherited')}</th>
            </tr>
          </thead>
          <tbody>
            {RATE_KEYS.map((k) => {
              const err = parsed[k] === undefined;
              return (
                <tr key={k} className="border-b border-border/60 last:border-0">
                  <td className="py-2.5 pr-3 align-middle">
                    <label htmlFor={`${uid}-${k}`} className="block text-sm font-medium">
                      {t(`${k}.label`)}
                    </label>
                    <span className="block text-xs text-muted-foreground">{t(`${k}.desc`)}</span>
                    {peaks !== undefined && (
                      <span className="mt-0.5 block text-[11px] text-muted-foreground" data-testid={`peak-${k}`}>
                        {peaks == null || peaks[k] === 0
                          ? t('peakNone')
                          : t('peak', {
                              value:
                                k === 'rpm' || k === 'tpm'
                                  ? `${fmtPeak(k, peaks[k])} ${t(`${k}.unit`)}`
                                  : `${fmtPeak(k, peaks[k])}${t(`${k}.per`)}`,
                            })}
                      </span>
                    )}
                  </td>
                  <td className="py-2.5 pr-3 align-middle">
                    <div className="relative w-56">
                      <input
                        id={`${uid}-${k}`}
                        inputMode={INT_KEYS.includes(k) ? 'numeric' : 'decimal'}
                        value={fields[k]}
                        onChange={(e) => setFields((f) => ({ ...f, [k]: e.target.value }))}
                        placeholder={t('inheritPlaceholder')}
                        aria-invalid={err || undefined}
                        aria-describedby={`${uid}-${k}-inh`}
                        className="w-full rounded-md border border-input bg-background py-1.5 pl-3 pr-[5.5rem] text-sm tabular-nums focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring aria-[invalid=true]:border-destructive"
                      />
                      <span className="pointer-events-none absolute inset-y-0 right-2.5 flex items-center text-xs text-muted-foreground">
                        {t(`${k}.unit`)}
                      </span>
                    </div>
                    {err && (
                      <span role="alert" className="mt-1 block text-xs text-destructive">
                        {INT_KEYS.includes(k) ? t('invalidInt') : t('invalidMoney')}
                      </span>
                    )}
                  </td>
                  <td id={`${uid}-${k}-inh`} className="py-2.5 align-middle text-xs text-muted-foreground">
                    {inheritedText(k)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </EditPolicyDialog>
  );
}
