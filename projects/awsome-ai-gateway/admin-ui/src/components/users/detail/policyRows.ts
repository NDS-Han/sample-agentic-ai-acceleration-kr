// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 팀·사용자 상세의 "적용 중인 정책" 요약 줄 — 순수 계산(표시 문구는 컴포넌트).
 *
 * 출처(source)는 보는 대상 기준이다:
 *   - own  : 이 대상(팀 또는 사용자)에 직접 설정
 *   - team : 사용자 뷰에서 팀 정책을 물려받음
 *   - org  : 조직 기본값을 물려받음(앱 접근의 조직 정책, rate limit 의 GLOBAL, 웹 검색 조직 기본)
 *   - none : 어디에도 설정 없음 = 제한 없음
 *
 * 입력은 유효 정책(GET /admin/{users|teams}/{id}/effective-policy) 하나 — 같은 응답이
 * 유효 정책 타일·차단 모델 칩에도 쓰이므로 화면 전체가 한 출처를 본다.
 */

import type {
  EffectivePolicy,
  EffectiveRateLimitEntry,
  ModelListItem,
  WebSearchQuotaEffective,
} from '@/types/entities';

export type PolicyScope = 'team' | 'user';
export type PolicySource = 'own' | 'team' | 'org' | 'none';

// ─── 앱 접근 ──────────────────────────────────────────────────────────────────

export interface AppsSummary {
  /** null = 제한 없음(모든 앱). */
  allowed: string[] | null;
  source: PolicySource;
}

export function appsSummary(p: EffectivePolicy, scope: PolicyScope): AppsSummary {
  const src = p.allowed_clients_source;
  const source: PolicySource =
    src === 'user' ? 'own' : src === 'team' ? (scope === 'team' ? 'own' : 'team') : src === 'organization' ? 'org' : 'none';
  const list = p.allowed_clients;
  // 출처가 없거나 목록이 비면 제한 없음. 세 앱을 모두 담은 명시 목록도 값은 "모든 앱"이지만
  // 출처(own/team/org)는 그대로 보인다 — 상속을 끊은 정책이라는 사실이 중요하다.
  const allowed = source === 'none' || !list || list.length === 0 ? null : [...list];
  return { allowed, source };
}

// ─── 허용 모델 ────────────────────────────────────────────────────────────────

export interface ModelsSummary {
  allowedCount: number;
  total: number;
  /** 차단된 별칭(카탈로그 순서). */
  blocked: string[];
  source: PolicySource;
}

export function modelsSummary(p: EffectivePolicy, catalog: ModelListItem[], scope: PolicyScope): ModelsSummary {
  const src = p.allowed_models_source;
  const source: PolicySource =
    src === 'user' ? 'own' : src === 'team' ? (scope === 'team' ? 'own' : 'team') : 'none';
  const aliases = catalog.map((m) => m.alias);
  const allow = p.allowed_models == null || p.allowed_models.length === 0 ? null : new Set(p.allowed_models);
  // 별칭 기준 — 백엔드 유효 정책(cells)과 같은 판정이라 화면의 차단 칩과 숫자가 어긋나지 않는다.
  const isAllowed = (m: ModelListItem) => allow == null || allow.has(m.alias);
  const blocked = catalog.filter((m) => !isAllowed(m)).map((m) => m.alias);
  return { allowedCount: aliases.length - blocked.length, total: aliases.length, blocked, source };
}

// ─── Rate limit ───────────────────────────────────────────────────────────────

export interface RateValues {
  rpm: number | null;
  tpm: number | null;
  cpm: number | null;
  cph: number | null;
}

export const RATE_KEYS = ['rpm', 'tpm', 'cpm', 'cph'] as const;
export type RateKey = (typeof RATE_KEYS)[number];

const EMPTY_RATE: RateValues = { rpm: null, tpm: null, cpm: null, cph: null };

function num(v: string | number | null | undefined): number | null {
  if (v == null) return null;
  const n = typeof v === 'number' ? v : Number(v);
  return Number.isFinite(n) ? n : null;
}

function entryValues(e: EffectiveRateLimitEntry | undefined): RateValues | null {
  if (!e) return null;
  return { rpm: num(e.rpm_limit), tpm: num(e.tpm_limit), cpm: num(e.cpm_limit_usd), cph: num(e.cph_limit_usd) };
}

export interface RateSummary {
  own: RateValues | null;
  /** 사용자 뷰에서만 — 팀 설정. */
  team: RateValues | null;
  /** 조직(GLOBAL) 설정. */
  org: RateValues | null;
  source: PolicySource;
  /** 화면 요약 값 — 각 항목에 실제로 걸리는 가장 가까운 레벨 값(own → team → org). */
  effective: RateValues;
}

export function rateSummary(p: EffectivePolicy, scope: PolicyScope): RateSummary {
  const general = p.rate_limits.filter((r) => r.model_alias == null);
  const pick = (s: string) => entryValues(general.find((r) => r.scope === s));
  const own = pick(scope === 'user' ? 'USER' : 'TEAM');
  const team = scope === 'user' ? pick('TEAM') : null;
  const org = pick('GLOBAL');
  const source: PolicySource = own ? 'own' : team ? 'team' : org ? 'org' : 'none';
  const effective = { ...EMPTY_RATE };
  for (const k of RATE_KEYS) effective[k] = own?.[k] ?? team?.[k] ?? org?.[k] ?? null;
  return { own, team, org, source, effective };
}

export function rateIsUnlimited(v: RateValues): boolean {
  return RATE_KEYS.every((k) => v[k] == null);
}

// ─── 웹 검색 한도 ─────────────────────────────────────────────────────────────

export interface WebSearchSummary {
  /** 사용자 뷰: 개인 월 한도 · 팀 뷰: 팀 공용 풀. null = 무제한. */
  monthlyLimit: number | null;
  used: number;
  perRequestCap: number;
  source: PolicySource;
}

export function webSearchSummary(q: WebSearchQuotaEffective | null | undefined, scope: PolicyScope): WebSearchSummary | null {
  if (!q) return null;
  if (scope === 'team') {
    const own = q.team_pool_limit != null || q.per_request_cap_source === 'team';
    return {
      monthlyLimit: q.team_pool_limit,
      used: q.team_used,
      perRequestCap: q.per_request_cap,
      source: own ? 'own' : q.per_request_cap_source === 'organization' ? 'org' : 'none',
    };
  }
  const s = q.monthly_limit_source;
  return {
    monthlyLimit: q.monthly_limit,
    used: q.used,
    perRequestCap: q.per_request_cap,
    source:
      s === 'user' || q.per_request_cap_source === 'user'
        ? 'own'
        : q.per_request_cap_source === 'team'
          ? 'team'
          : s === 'organization' || q.per_request_cap_source === 'organization'
            ? 'org'
            : 'none',
  };
}

// ─── 예산 ─────────────────────────────────────────────────────────────────────

export interface BudgetSummary {
  /** 이 대상의 총액 한도(사용자: 개인 cap, 팀: 팀 예산). null = 없음. */
  max: number | null;
  used: number | null;
  /** 사용자 뷰 — 함께 쓰는 팀 예산. */
  teamMax: number | null;
  teamUsed: number | null;
  source: PolicySource;
}

export function budgetSummary(p: EffectivePolicy, scope: PolicyScope): BudgetSummary {
  const total = (s: string) => p.budgets.find((b) => b.scope === s && b.client === null);
  const own = total(scope === 'user' ? 'USER' : 'TEAM');
  const team = scope === 'user' ? total('TEAM') : own;
  return {
    max: num(own?.max_budget_usd),
    used: num(own?.used_usd),
    teamMax: num(team?.max_budget_usd),
    teamUsed: num(team?.used_usd),
    source: own ? 'own' : scope === 'user' && team ? 'team' : 'none',
  };
}

// ─── 자동 다운그레이드 ────────────────────────────────────────────────────────

export interface DowngradeSummary {
  steps: { threshold: number; to: string; from: string }[];
  source: PolicySource;
}

export function downgradeSummary(p: EffectivePolicy, scope: PolicyScope): DowngradeSummary {
  const rules = [...p.downgrade_rules].sort((a, b) => a.threshold_pct - b.threshold_pct);
  const ownScope = scope === 'user' ? 'USER' : 'TEAM';
  const has = (s: string) => rules.some((r) => r.scope === s);
  return {
    steps: rules.map((r) => ({ threshold: r.threshold_pct, from: r.from_model_alias, to: r.to_model_alias })),
    source: has(ownScope) ? 'own' : scope === 'user' && has('TEAM') ? 'team' : 'none',
  };
}

// ─── 유효 정책 요약(사용자) ───────────────────────────────────────────────────

export interface BlockedModel {
  alias: string;
  /** 거부 축 — "user_app" | "user_model" | "model_app" (EffectivePolicyCell.blocked_by). */
  reasons: string[];
}

/** 어느 앱에서도 쓸 수 없는 모델 + 막은 정책 축. 일부 앱에서만 막힌 모델은 매트릭스가 보여준다. */
export function blockedModels(p: EffectivePolicy): { allowed: string[]; blocked: BlockedModel[] } {
  const byModel = new Map<string, { any: boolean; reasons: Set<string> }>();
  for (const c of p.cells) {
    const e = byModel.get(c.model_alias) ?? { any: false, reasons: new Set<string>() };
    if (c.allowed) e.any = true;
    else c.blocked_by.forEach((r) => e.reasons.add(r));
    byModel.set(c.model_alias, e);
  }
  const allowed: string[] = [];
  const blocked: BlockedModel[] = [];
  for (const [alias, e] of [...byModel.entries()].sort((a, b) => a[0].localeCompare(b[0]))) {
    if (e.any) allowed.push(alias);
    else blocked.push({ alias, reasons: [...e.reasons].sort() });
  }
  return { allowed, blocked };
}

/** 다운그레이드 체인에 있지만 이 대상이 쓸 수 없는 모델(기존 EffectivePolicyCard 경고와 같은 규칙). */
export function unavailableChainModels(p: EffectivePolicy): string[] {
  const chain = [...new Set(p.downgrade_rules.flatMap((r) => [r.from_model_alias, r.to_model_alias]))];
  return chain.filter((a) => !p.cells.some((c) => c.model_alias === a && c.allowed));
}
