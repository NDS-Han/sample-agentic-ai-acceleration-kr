'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useEffect, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitScope } from '@/types/enums';
import { setRateLimitAction } from '@/lib/actions/rate-limits';
import { fetchRateLimitUsage, type RateLimitUsage } from '@/lib/utils/rateLimitUsage';
import { UsageTrendChart } from '@/components/rate-limits/UsageTrendChart';
import { FormError } from '@/components/common/FormError';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';

interface RateLimitConfigPanelProps {
  node: RateLimitTreeNode | null;
  /** 미저장 변경 여부 통지 — 부모가 노드 전환 시 확인 다이얼로그를 띄우는 데 쓴다. */
  onDirtyChange?: (dirty: boolean) => void;
}

// SCOPE_LABEL is now resolved inside the component via translations

function parsePosInt(val: string): number | null {
  const n = parseInt(val, 10);
  return isNaN(n) || n <= 0 ? null : n;
}

function parsePosFloat(val: string): number | null {
  const n = parseFloat(val);
  return isNaN(n) || n <= 0 ? null : n;
}

export function RateLimitConfigPanel({ node, onDirtyChange }: RateLimitConfigPanelProps) {
  const t = useTranslations('rateLimits');
  const tCommon = useTranslations('common');
  const { toast } = useToast();

  const SCOPE_LABEL: Record<string, string> = {
    GLOBAL: t('scopeLabel.GLOBAL'),
    TEAM: t('scopeLabel.TEAM'),
    USER: t('scopeLabel.USER'),
  };
  const [isPending, startTransition] = useTransition();
  const [error, setError] = useState<string | null>(null);

  const [rpm, setRpm] = useState('');
  const [tpm, setTpm] = useState('');
  const [cpm, setCpm] = useState('');
  const [cph, setCph] = useState('');
  // dirty 판정 기준 — 로드/저장 성공 시점의 값. 필드와 다른 문자열이면 미저장 변경.
  const [baseline, setBaseline] = useState({ rpm: '', tpm: '', cpm: '', cph: '' });
  const [usage, setUsage] = useState<RateLimitUsage | null>(null);

  // node가 변경될 때 폼 값 초기화
  useEffect(() => {
    const loaded = node?.config
      ? {
          rpm: node.config.rpm != null ? String(node.config.rpm) : '',
          tpm: node.config.tpm != null ? String(node.config.tpm) : '',
          cpm: node.config.cpm != null ? String(node.config.cpm) : '',
          cph: node.config.cph != null ? String(node.config.cph) : '',
        }
      : { rpm: '', tpm: '', cpm: '', cph: '' };
    setRpm(loaded.rpm);
    setTpm(loaded.tpm);
    setCpm(loaded.cpm);
    setCph(loaded.cph);
    setBaseline(loaded);
    setError(null);
  }, [node]);

  const isDirty =
    rpm !== baseline.rpm || tpm !== baseline.tpm || cpm !== baseline.cpm || cph !== baseline.cph;

  // 부모(트리 뷰)에 dirty 상태 전달 — 노드 전환 전 확인에 사용.
  useEffect(() => {
    onDirtyChange?.(isDirty);
  }, [isDirty, onDirtyChange]);

  // 실시간 RPM 사용량(§60.9) — node 선택 시 + 10초마다 폴링(gateway-proxy Redis 카운터).
  useEffect(() => {
    if (!node || node.scope === RateLimitScope.GLOBAL) {
      setUsage(null);
      return;
    }
    let alive = true;
    const load = () =>
      fetchRateLimitUsage(node.scope, node.id)
        .then((u) => { if (alive) setUsage(u); })
        .catch(() => { if (alive) setUsage(null); });
    load();
    const t = setInterval(load, 10_000);
    return () => { alive = false; clearInterval(t); };
  }, [node]);

  if (!node) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        {t('selectNodePrompt')}
      </div>
    );
  }

  const isUserOrTeam =
    node.scope === RateLimitScope.USER || node.scope === RateLimitScope.TEAM;

  const inheritedRpm = node.inherited_from && node.config?.rpm != null
    ? String(node.config.rpm)
    : undefined;
  const inheritedTpm = node.inherited_from && node.config?.tpm != null
    ? String(node.config.tpm)
    : undefined;

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);

    startTransition(async () => {
      const result = await setRateLimitAction({
        target_id: node.id,
        scope: node.scope,
        rpm: parsePosInt(rpm),
        tpm: parsePosInt(tpm),
        cpm: isUserOrTeam ? parsePosFloat(cpm) : undefined,
        cph: isUserOrTeam ? parsePosFloat(cph) : undefined,
      });

      if (result.success) {
        // 저장 성공 시점의 입력값을 새 기준으로 — dirty 해제.
        setBaseline({ rpm, tpm, cpm, cph });
        toast({
          type: 'success',
          message: t('saved', { label: node.label }),
          auto_dismiss_ms: 3000,
        });
      } else {
        setError(result.error);
      }
    });
  };

  return (
    <div>
      {/* 헤더 */}
      <div className="flex items-center gap-3 mb-4">
        <h2 className="text-lg font-semibold">{node.label}</h2>
        <span className="inline-flex items-center rounded-full border px-2.5 py-0.5 text-xs font-semibold">
          {SCOPE_LABEL[node.scope] ?? node.scope}
        </span>
      </div>

      {/* 상속 정보 */}
      {node.inherited_from && (
        <div className="mb-4 rounded-md bg-muted px-3 py-2 text-sm text-muted-foreground">
          {t('inheritingFrom')} <span className="font-medium text-foreground">{node.inherited_from}</span>
        </div>
      )}

      {/* 실시간 사용량(§60.9) — gateway-proxy Redis sliding-window 카운터. 10초 폴링. */}
      {usage?.available && (
        <div className="mb-4 rounded-md border border-border bg-card/50 px-3 py-2.5">
          <div className="flex items-center justify-between">
            <span className="text-xs font-medium text-muted-foreground">
              {t('liveRpm', { seconds: usage.window_sec })}
            </span>
            <span className="num text-sm font-semibold text-foreground">
              {usage.rpm_used_total}
              {node.config?.rpm != null && (
                <span className="text-muted-foreground font-normal"> / {node.config.rpm}</span>
              )}
            </span>
          </div>
          {/* 한도 대비 게이지(설정 있을 때만) */}
          {node.config?.rpm != null && node.config.rpm > 0 && (
            <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-primary transition-all"
                style={{ width: `${Math.min(100, (usage.rpm_used_total / node.config.rpm) * 100)}%` }}
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
          {usage.rpm_used_total === 0 && (
            <p className="mt-1 text-[11px] text-muted-foreground">{t('noRecentRequests', { seconds: usage.window_sec })}</p>
          )}
        </div>
      )}

      {/* 과거 사용량 트렌드 — 한도 설정의 근거. usage_logs 버킷 집계를
          한도 단위(분당/시간당)로 정규화해 설정값 기준선과 함께 표시. */}
      {isUserOrTeam && (
        <UsageTrendChart
          scope={node.scope}
          scopeId={node.id}
          limits={{
            rpm: node.config?.rpm,
            tpm: node.config?.tpm,
            cph: node.config?.cph,
          }}
        />
      )}

      <form onSubmit={handleSubmit} className="space-y-4">
        {/* RPM */}
        <div className="space-y-1">
          <label className="text-sm font-medium">RPM (Requests Per Minute)</label>
          <input
            type="number"
            min={1}
            value={rpm}
            onChange={(e) => setRpm(e.target.value)}
            placeholder={inheritedRpm ?? t('unlimited')}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <p className="text-xs text-muted-foreground">{t('emptyMeansUnlimited')}</p>
        </div>

        {/* TPM */}
        <div className="space-y-1">
          <label className="text-sm font-medium">TPM (Tokens Per Minute)</label>
          <input
            type="number"
            min={1}
            value={tpm}
            onChange={(e) => setTpm(e.target.value)}
            placeholder={inheritedTpm ?? t('unlimited')}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
          <p className="text-xs text-muted-foreground">{t('emptyMeansUnlimited')}</p>
        </div>

        {/* CPM — USER/TEAM only */}
        <div className="space-y-1">
          <label
            className={[
              'text-sm font-medium',
              !isUserOrTeam ? 'text-muted-foreground' : '',
            ].join(' ')}
          >
            CPM (Cost Per Minute, USD)
          </label>
          <input
            type="number"
            min={0}
            step="0.0001"
            value={cpm}
            onChange={(e) => setCpm(e.target.value)}
            placeholder={t('unlimited')}
            disabled={!isUserOrTeam}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50 disabled:cursor-not-allowed"
          />
          {!isUserOrTeam && (
            <p className="text-xs text-muted-foreground">{t('scopeOnlyUserTeam')}</p>
          )}
        </div>

        {/* CPH — USER/TEAM only */}
        <div className="space-y-1">
          <label
            className={[
              'text-sm font-medium',
              !isUserOrTeam ? 'text-muted-foreground' : '',
            ].join(' ')}
          >
            CPH (Cost Per Hour, USD)
          </label>
          <input
            type="number"
            min={0}
            step="0.0001"
            value={cph}
            onChange={(e) => setCph(e.target.value)}
            placeholder={t('unlimited')}
            disabled={!isUserOrTeam}
            className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50 disabled:cursor-not-allowed"
          />
          {!isUserOrTeam && (
            <p className="text-xs text-muted-foreground">{t('scopeOnlyUserTeam')}</p>
          )}
        </div>

        <FormError error={error} />

        <div className="flex items-center justify-end gap-3 pt-2">
          {isDirty && (
            <span className="text-xs text-amber-600 dark:text-amber-400">
              {t('unsavedChanges')}
            </span>
          )}
          <SpinnerButton type="submit" isLoading={isPending}>
            {tCommon('save')}
          </SpinnerButton>
        </div>
      </form>
    </div>
  );
}