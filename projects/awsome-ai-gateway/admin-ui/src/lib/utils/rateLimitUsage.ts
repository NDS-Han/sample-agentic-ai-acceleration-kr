// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// 실시간 RPM 사용량(§60.9) 클라이언트 폴링 — /users 의 LiveRpmCard(Rate limit 사용량 카드)에서 사용.
// gateway-proxy 가 Redis 에 적재하는 sliding-window 카운터를 /api/rate-limits/usage
// 프록시 경유로 읽는다. fail-soft: 실패 시 available:false.

export interface RateLimitUsage {
  available: boolean;
  scope?: string;
  scope_id?: string;
  window_sec: number;
  /** RPM 한도가 설정된 scope 에만 proxy 가 카운터를 적재한다. false 면
   *  rpm_used_total 은 "요청 없음"이 아니라 "계량 안 함"이므로 UI 가
   *  0 대신 한도 미설정 상태로 렌더해야 한다. null/undefined = 미상. */
  tracked?: boolean | null;
  rpm_used_total: number;
  by_model: { model_alias: string; rpm_used: number }[];
  reason?: string;
}

export async function fetchRateLimitUsage(scope: string, scopeId: string): Promise<RateLimitUsage> {
  const params = new URLSearchParams({ scope, scope_id: scopeId });
  try {
    const res = await fetch(`/api/rate-limits/usage?${params}`, { cache: 'no-store' });
    const data = await res.json();
    return {
      available: !!data.available,
      scope: data.scope,
      scope_id: data.scope_id,
      window_sec: data.window_sec ?? 60,
      tracked: data.tracked ?? null,
      rpm_used_total: data.rpm_used_total ?? 0,
      by_model: Array.isArray(data.by_model) ? data.by_model : [],
      reason: data.reason,
    };
  } catch {
    return { available: false, window_sec: 60, rpm_used_total: 0, by_model: [] };
  }
}

// ── 사용량 트렌드(usage_logs 버킷 집계) ──
// UsageTrendChart(/users Rate limit 사용량 카드)·Rate limit 대화상자의 24시간 최대치용. 버킷당 원시값(requests/tokens/cost_usd)을
// 돌려주고, 분당·시간당 정규화는 표시 쪽에서 bucket_sec 으로 계산한다.

export interface UsageTrendPoint {
  t: number; // epoch seconds (버킷 시작, UTC)
  requests: number;
  tokens: number;
  cost_usd: number;
  partial?: boolean; // 진행 중인(아직 다 안 찬) 마지막 버킷 — 서버가 표시
}

export interface UsageTrend {
  available: boolean;
  window_sec: number;
  bucket_sec: number;
  points: UsageTrendPoint[];
  reason?: string;
}

export type TrendWindow = '1h' | '6h' | '24h' | '7d';

export async function fetchRateLimitTrend(
  scope: string,
  scopeId: string,
  window: TrendWindow,
): Promise<UsageTrend> {
  const params = new URLSearchParams({ scope, scope_id: scopeId, window });
  try {
    const res = await fetch(`/api/rate-limits/usage-trend?${params}`, { cache: 'no-store' });
    const data = await res.json();
    return {
      available: !!data.available,
      window_sec: data.window_sec ?? 0,
      bucket_sec: data.bucket_sec ?? 60,
      points: Array.isArray(data.points) ? data.points : [],
      reason: data.reason,
    };
  } catch {
    return { available: false, window_sec: 0, bucket_sec: 60, points: [] };
  }
}

/** 구간 최대치 — 분당 요청·분당 토큰·분당 비용·시간당 비용(버킷 크기로 정규화). 데이터 없으면 null. */
export interface TrendPeaks {
  rpm: number;
  tpm: number;
  cpm: number;
  cph: number;
}

export function trendPeaks(trend: UsageTrend | null | undefined): TrendPeaks | null {
  if (!trend?.available || trend.points.length === 0 || !(trend.bucket_sec > 0)) return null;
  const b = trend.bucket_sec;
  const peak = { rpm: 0, tpm: 0, cpm: 0, cph: 0 };
  for (const p of trend.points) {
    peak.rpm = Math.max(peak.rpm, (p.requests * 60) / b);
    peak.tpm = Math.max(peak.tpm, (p.tokens * 60) / b);
    peak.cpm = Math.max(peak.cpm, (p.cost_usd * 60) / b);
    peak.cph = Math.max(peak.cph, (p.cost_usd * 3600) / b);
  }
  return peak;
}
