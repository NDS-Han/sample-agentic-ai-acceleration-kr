'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 실시간 RPM 카드 — "최근 60초 요청 수"(§60.9). 예전 ScopeRateLimitPanel 안의 표시를 그대로
 * 떼어 낸 것(같은 메시지·같은 규칙): 10초마다 /api/rate-limits/usage 폴링 — 탭이 보일 때만,
 * 401 이면 멈추고 로그인으로.
 *
 * tracked=false(RPM 한도 미설정)면 proxy 가 카운터를 적재하지 않는다 — 0 을 "요청 없음"으로
 * 오독하지 않게 '—' + 설명. 한도가 있으면 막대.
 */

import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';

import { fetchRateLimitUsage, type RateLimitUsage } from '@/lib/utils/rateLimitUsage';
import { redirectToLoginIfUnauthorized } from '@/lib/utils/unauthorized';

/** 폴링 주기. 탭이 숨겨져 있으면 멈추고, 다시 보이면 즉시 한 번 읽고 재개한다. */
export const LIVE_RPM_POLL_MS = 10_000;

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
    let stopped = false;
    let timer: ReturnType<typeof setInterval> | null = null;
    let ctrl: AbortController | null = null;
    const stop = () => {
      if (timer) clearInterval(timer);
      timer = null;
    };
    const load = () => {
      // 직전 요청이 아직이면 끊고 새로 — 느린 응답이 쌓이지 않게.
      ctrl?.abort();
      const c = new AbortController();
      ctrl = c;
      fetchRateLimitUsage(scope, scopeId, c.signal)
        .then((u) => {
          if (!alive || c.signal.aborted) return;
          if (u.unauthorized) {
            // 세션 만료 — 폴링을 영구 중단하고 로그인으로.
            stopped = true;
            stop();
            redirectToLoginIfUnauthorized({ status: 401 });
            return;
          }
          setUsage(u);
        })
        .catch(() => {
          if (alive && !c.signal.aborted) setUsage(null);
        });
    };
    const start = () => {
      if (stopped || timer) return;
      load();
      timer = setInterval(load, LIVE_RPM_POLL_MS);
    };
    // 숨겨진 탭에서는 폴링하지 않는다 — 열어 둔 /users 탭들이 10초마다 admin-api·Redis 를
    // 치던 부하를 없앤다. 다시 보이면 즉시 갱신.
    const onVisibility = () => {
      if (document.visibilityState === 'hidden') {
        stop();
        ctrl?.abort();
      } else {
        start();
      }
    };
    if (document.visibilityState !== 'hidden') start();
    document.addEventListener('visibilitychange', onVisibility);
    return () => {
      alive = false;
      stop();
      ctrl?.abort();
      document.removeEventListener('visibilitychange', onVisibility);
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
