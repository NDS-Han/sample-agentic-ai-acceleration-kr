'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 실시간 RPM 카드 — "최근 60초 요청 수"(§60.9). 예전 ScopeRateLimitPanel 안의 표시를 그대로
 * 떼어 낸 것(같은 메시지·같은 규칙): 10초마다 /api/rate-limits/usage 폴링.
 *
 * tracked=false(RPM 한도 미설정)면 proxy 가 카운터를 적재하지 않는다 — 0 을 "요청 없음"으로
 * 오독하지 않게 '—' + 설명. 한도가 있으면 막대.
 */

import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';

import { fetchRateLimitUsage, type RateLimitUsage } from '@/lib/utils/rateLimitUsage';

export function LiveRpmCard({
  scope,
  scopeId,
  rpmLimit,
  inherited = false,
}: {
  scope: 'USER' | 'TEAM';
  scopeId: string;
  /** 적용 중인 RPM 한도(없으면 null) — 막대·분모. */
  rpmLimit: number | null;
  /** 사용자 한도가 팀에서 상속된 경우 — 미계량 설명 문구가 다르다. */
  inherited?: boolean;
}) {
  const t = useTranslations('users.rateLimit');
  const [usage, setUsage] = useState<RateLimitUsage | null>(null);

  useEffect(() => {
    let alive = true;
    const load = () =>
      fetchRateLimitUsage(scope, scopeId)
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
  }, [scope, scopeId]);

  if (!usage?.available) return null;
  return (
    <div className="rounded-md border border-border bg-card/50 px-3 py-2.5" data-testid="live-rpm">
      <div className="flex items-center justify-between">
        <span className="text-xs font-medium text-muted-foreground">{t('liveRpm', { seconds: usage.window_sec })}</span>
        {usage.tracked === false ? (
          <span className="text-sm font-semibold text-muted-foreground">—</span>
        ) : (
          <span className="num text-sm font-semibold text-foreground" data-testid="live-rpm-value">
            {usage.rpm_used_total}
            {rpmLimit != null && <span className="font-normal text-muted-foreground"> / {rpmLimit}</span>}
          </span>
        )}
      </div>
      {usage.tracked !== false && rpmLimit != null && rpmLimit > 0 && (
        <div className="mt-1.5 h-1.5 w-full overflow-hidden rounded-full bg-muted" data-testid="live-rpm-bar">
          <div
            className="h-full rounded-full bg-primary transition-all"
            style={{ width: `${Math.min(100, (usage.rpm_used_total / rpmLimit) * 100)}%` }}
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
          {inherited ? t('untrackedHintInherited') : t('untrackedHint')}
        </p>
      ) : (
        usage.rpm_used_total === 0 && (
          <p className="mt-1 text-[11px] text-muted-foreground">{t('noRecentRequests', { seconds: usage.window_sec })}</p>
        )
      )}
    </div>
  );
}
