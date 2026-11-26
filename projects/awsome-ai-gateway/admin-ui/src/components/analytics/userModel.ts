// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 사용자 × 모델 토큰 사용량 — 순수 헬퍼·와이어 타입.
 *
 * 컴포넌트(UserModelSection/Table/Drawer)는 이 파일의 함수만으로 셀 값·히트 농도·
 * 정렬 파라미터·조회 쿼리를 만든다 — 렌더 없이 단위 테스트할 수 있게 분리.
 */

import { fmtTokensCompact, fmtUsdExact } from '@/lib/utils/format';
import { redirectToLoginIfUnauthorized } from '@/lib/utils/unauthorized';

// ─── wire types (admin-api /admin/analytics/user-models) ───────────────────────

export interface UserModelCell {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
  cost_usd: number;
  requests: number;
}

export interface UserModelColumn {
  model: string;
  display_name: string | null;
  total_tokens: number;
}

export type TokenMode = 'total' | 'input' | 'output' | 'cache';

export interface UserModelRow {
  user_id: string;
  name: string | null;
  email: string | null;
  team_name: string | null;
  /** top_models 중 사용한 모델만 키가 있다 — 키 없음 = 미사용(—). */
  cells: Record<string, UserModelCell>;
  /** top 외 모델 합계 — 없으면 null. */
  other: UserModelCell | null;
  total: UserModelCell;
}

export interface UserModelResponse {
  period: string;
  timezone: string;
  start_date: string;
  end_date: string;
  top_models: UserModelColumn[];
  other_models: UserModelColumn[];
  total_users: number;
  page: number;
  page_size: number;
  sort: string;
  dir: SortDir;
  q: string;
  /** 모드별 범위 전체(모든 페이지)의 최대 top-모델 셀 — 히트 농도 분모. */
  max_cell: Record<TokenMode, number>;
  /** 범위(검색 반영) 전체 합계 — 모든 페이지의 행 합. 구 admin-api 는 없다. */
  scope_total?: UserModelCell;
  rows: UserModelRow[];
}

export interface UserModelDetailModel extends UserModelCell {
  model: string;
  display_name: string | null;
  avg_latency_ms: number | null;
}

export interface UserModelDetail {
  user_id: string;
  name: string | null;
  email: string | null;
  team_name: string | null;
  period: string;
  timezone: string;
  start_date: string;
  end_date: string;
  models: UserModelDetailModel[];
  total: UserModelCell & { avg_latency_ms: number | null };
}

// ─── filters → query ──────────────────────────────────────────────────────────

/** 페이지가 다른 analytics 호출에 쓰는 것과 같은 값(월/custom 구간/scope/client). */
export interface UserModelFilters {
  period: string;
  start_date?: string | null;
  end_date?: string | null;
  scope: string;
  client?: string | null;
}

export function filterParams(f: UserModelFilters): URLSearchParams {
  const p = new URLSearchParams({ period: f.period, scope: f.scope || 'all' });
  // 두 날짜가 다 있을 때만 구간 — 한쪽만 보내면 API 가 400.
  if (f.start_date && f.end_date) {
    p.set('start_date', f.start_date);
    p.set('end_date', f.end_date);
  }
  if (f.client && f.client !== 'all') p.set('client', f.client);
  return p;
}

// ─── sort ─────────────────────────────────────────────────────────────────────

export type SortDir = 'asc' | 'desc';
/** 표의 정렬 열 — 모델 열은 'model:<alias>'. */
export type SortColumn = 'name' | 'total' | 'cost' | 'other' | `model:${string}`;

export interface SortState {
  col: SortColumn;
  dir: SortDir;
}

export const DEFAULT_SORT: SortState = { col: 'total', dir: 'desc' };

/**
 * 표 열 → 서버 sort 파라미터. Total 열은 활성 토큰 모드를 따른다(Input 모드에서
 * Total 로 정렬하면 사용자 입력 토큰 합계순). 모델 열은 서버가 그 모델의 total 로 정렬.
 */
export function toSortParam(col: SortColumn, mode: TokenMode): string {
  return col === 'total' ? mode : col;
}

/** 헤더 클릭 — 같은 열이면 방향 반전, 새 열이면 이름은 오름차순·숫자는 내림차순부터. */
export function nextSort(prev: SortState, col: SortColumn): SortState {
  if (prev.col === col) return { col, dir: prev.dir === 'desc' ? 'asc' : 'desc' };
  return { col, dir: col === 'name' ? 'asc' : 'desc' };
}

// ─── cells ────────────────────────────────────────────────────────────────────

export function cellValue(cell: UserModelCell | null | undefined, mode: TokenMode): number {
  if (!cell) return 0;
  switch (mode) {
    case 'input':
      return Number(cell.input_tokens) || 0;
    case 'output':
      return Number(cell.output_tokens) || 0;
    case 'cache':
      return (Number(cell.cache_read_tokens) || 0) + (Number(cell.cache_write_tokens) || 0);
    default:
      return Number(cell.total_tokens) || 0;
  }
}

/**
 * 히트 농도 0..1 — sqrt 스케일(긴 꼬리 분포에서 작은 셀도 구분되게). 분모는 범위
 * 전체의 max_cell 이라 페이지를 넘겨도 같은 값은 같은 농도.
 */
export function heatIntensity(value: number, max: number): number {
  if (!(value > 0) || !(max > 0)) return 0;
  return Math.min(1, Math.max(0, Math.sqrt(value / max)));
}

// 농도 → primary 틴트 알파. 상한 0.46 — 라이트(#0d9488 위 진한 글자)·다크(#2dd4bf
// 위 밝은 글자) 모두 본문 대비 4.5:1 이상을 유지하는 지점.
const HEAT_MIN_ALPHA = 0.06;
const HEAT_MAX_ALPHA = 0.46;

export function heatBackground(intensity: number): string | undefined {
  if (!(intensity > 0)) return undefined;
  const a = HEAT_MIN_ALPHA + (HEAT_MAX_ALPHA - HEAT_MIN_ALPHA) * Math.min(1, intensity);
  return `hsl(var(--primary) / ${a.toFixed(3)})`;
}

/** 토큰 수 — 0 은 '0'(미사용 '—' 과 구분), 그 외는 앱 공용 K/M/B. */
export function fmtTok(n: number | null | undefined): string {
  const v = Number(n) || 0;
  return v > 0 ? fmtTokensCompact(v) : '0';
}

export function fmtInt(n: number | null | undefined): string {
  return (Number(n) || 0).toLocaleString('en-US');
}

export interface CellTipLabels {
  input: string;
  output: string;
  cacheRead: string;
  cacheWrite: string;
  total: string;
  /** 요청 수 문구 — 복수형은 메시지(ICU)가 정한다. 예: "4 requests". */
  requests: (_count: number) => string;
}

/**
 * 셀 툴팁 — 제목 줄 + 버킷별 분해(정확값) + 합계(표와 같은 K/M) + "비용 · 요청 수".
 * 금액은 정확값(1센트 미만 포함). 예: "… Total 143K\n$0.13 · 4 requests".
 */
export function cellTooltip(title: string, cell: UserModelCell, l: CellTipLabels): string {
  return [
    title,
    `${l.input} ${fmtInt(cell.input_tokens)} · ${l.output} ${fmtInt(cell.output_tokens)}`,
    `${l.cacheRead} ${fmtInt(cell.cache_read_tokens)} · ${l.cacheWrite} ${fmtInt(cell.cache_write_tokens)}`,
    `${l.total} ${fmtTok(cell.total_tokens)}`,
    `${fmtUsdExact(Number(cell.cost_usd) || 0)} · ${l.requests(Number(cell.requests) || 0)}`,
  ].join('\n');
}

/**
 * 이름 아래 보조 줄 — "email · team". 이름이 없으면 이름 자리에 이미 이메일이 나오므로
 * 이메일은 빼고 팀만(표 행·드로어 머리 공통 — 동명이인 구분용).
 */
export function userSubline(u: { name?: string | null; email?: string | null; team_name?: string | null }): string {
  return [u.name?.trim() ? u.email : null, u.team_name].filter(Boolean).join(' · ');
}

// ─── token mix ────────────────────────────────────────────────────────────────

// TokenMixDonutClient.TOKEN_BUCKETS 와 같은 버킷 색(어느 위젯이든 같은 버킷=같은 색).
// 그 파일은 recharts 를 끌고 오므로 여기선 값만 복제한다 — 바꿀 땐 둘 다.
export const MIX_BUCKETS = [
  { key: 'input_tokens', color: '#38bdf8' },
  { key: 'output_tokens', color: '#f472b6' },
  { key: 'cache_read_tokens', color: '#4ade80' },
  { key: 'cache_write_tokens', color: '#fbbf24' },
] as const;

/** 버킷별 비율(%) — 합 0 이면 모두 0. */
export function tokenMix(cell: UserModelCell): number[] {
  const vals = MIX_BUCKETS.map((b) => Number(cell[b.key]) || 0);
  const sum = vals.reduce((s, v) => s + v, 0);
  return vals.map((v) => (sum > 0 ? (v / sum) * 100 : 0));
}

// ─── fetch ────────────────────────────────────────────────────────────────────

export class UserModelFetchError extends Error {
  constructor(
    public status: number,
    public code?: string,
  ) {
    super(`user-models request failed: ${status}${code ? ` (${code})` : ''}`);
    this.name = 'UserModelFetchError';
  }
}

export async function fetchUserModelJson<T>(url: string, signal?: AbortSignal): Promise<T> {
  const res = await fetch(url, { signal, cache: 'no-store' });
  if (redirectToLoginIfUnauthorized(res)) throw new UserModelFetchError(401);
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as
      | { error?: { code?: string } | string; detail?: { code?: string } }
      | null;
    const code =
      (typeof body?.error === 'object' ? body.error?.code : undefined) ?? body?.detail?.code;
    throw new UserModelFetchError(res.status, code);
  }
  return (await res.json()) as T;
}

export function isRangeTooLongError(e: unknown): boolean {
  return e instanceof UserModelFetchError && (e.code === 'date_range_too_long' || e.status === 422);
}

// ─── Cost by User → drawer ────────────────────────────────────────────────────

/**
 * 'Cost by User' 막대(서버 컴포넌트 → 지연 로드 recharts)와 이 섹션은 트리상 형제라
 * 콜백을 직접 넘길 수 없다 — window CustomEvent 로 잇는다. 섹션은 By User 모드에서만
 * 마운트되므로 다른 모드에선 리스너가 없다(막대도 이벤트를 쏘지 않는다).
 */
export const USER_MODEL_OPEN_EVENT = 'analytics:user-model-open';

export interface UserModelTarget {
  user_id?: string | null;
  email?: string | null;
  name?: string | null;
}

export interface UserModelOpenDetail extends UserModelTarget {
  /** 닫힐 때 포커스를 돌려줄 요소. */
  trigger?: HTMLElement | null;
}

export function requestUserModelDrawer(detail: UserModelOpenDetail): void {
  if (typeof window === 'undefined') return;
  window.dispatchEvent(new CustomEvent<UserModelOpenDetail>(USER_MODEL_OPEN_EVENT, { detail }));
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function isUuid(v: string | null | undefined): v is string {
  return !!v && UUID_RE.test(v);
}
