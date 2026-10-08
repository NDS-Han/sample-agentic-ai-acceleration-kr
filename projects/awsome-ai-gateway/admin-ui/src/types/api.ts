// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { z } from 'zod';
import type { GroupByType, PeriodType, RateLimitScope, UserRole } from './enums';

// ─── Generic Response Wrappers ────────────────────────────────────────────────

export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
}

export interface APIError {
  status_code: number;
  error_code: string;
  message: string;
  details: unknown;
}

// ─── Virtual Keys ─────────────────────────────────────────────────────────────

export interface VirtualKeyCreateForm {
  user_id: string;
  expires_at: string | null; // ISO 8601 or null for no expiry
}

// ─── Budgets ──────────────────────────────────────────────────────────────────

export interface BudgetSetForm {
  target_id: string;
  target_type: 'TEAM' | 'USER';
  max_budget_usd: number;
  // undefined=보존(백엔드 optional-preserve) — 다이얼로그가 금액만 바꿔
  // 저장해도 enforcement 설정이 기본값으로 리셋되지 않는다.
  policy?: 'HARD_BLOCK' | 'SOFT_WARNING' | 'THROTTLE';
  alert_thresholds?: number[];
}

export const BudgetSetSchema = z.object({
  target_id: z.string().min(1, 'Target ID is required'),
  target_type: z.enum(['TEAM', 'USER']),
  max_budget_usd: z.number().nonnegative('Budget must be 0 or greater'),
  policy: z.enum(['HARD_BLOCK', 'SOFT_WARNING', 'THROTTLE']).optional(),
  alert_thresholds: z.array(z.number().int().min(1).max(100)).min(1).optional(),
  // TEAM 전용: 기본 유저 cap D — undefined=보존, null=해제(§3-1).
  default_user_cap_usd: z.number().nonnegative().nullish(),
});

// ─── Models ───────────────────────────────────────────────────────────────────

export interface ModelCreateForm {
  alias: string;
  provider: string;
  model_id: string;
  // OPENMODEL(vLLM) 등 커스텀 엔드포인트 모델용. Bedrock/Mantle 은 비워둔다(null 전송).
  endpoint_url?: string;
  input_price_per_1k: number;
  output_price_per_1k: number;
  cache_creation_5m_price_per_1k: number;
  cache_creation_1h_price_per_1k: number;
  cache_read_price_per_1k: number;
  description?: string;
  display_name?: string;
}

export const ModelCreateSchema = z.object({
  alias: z.string().min(1, 'Alias is required').max(64),
  provider: z.string().min(1, 'Provider is required'),
  model_id: z.string().min(1, 'Model ID is required'),
  endpoint_url: z.string().optional(),
  input_price_per_1k: z.number().nonnegative(),
  output_price_per_1k: z.number().nonnegative(),
  cache_creation_5m_price_per_1k: z.number().nonnegative().default(0),
  // 1h 캐시쓰기 단가 — 과거 스키마 누락으로 폼 값이 strip 되어 백엔드 default 0 으로
  // 박혀 1시간 캐시 사용분 청구가 누락되던 버그 수정(deepdive Q-pricing).
  cache_creation_1h_price_per_1k: z.number().nonnegative().default(0),
  cache_read_price_per_1k: z.number().nonnegative().default(0),
  // 스펙 필드 — optional. 컬럼은 model_aliases 에 존재(context_window/max_output_tokens,
  // litellm spec 동기화가 채움). 과거엔 폼/스키마 양쪽에 없어 required 로 두면 safeParse 가
  // 항상 실패했는데, 지금은 optional 입력란이 폼에 있으므로 optional 로 둔다.
  // 수정 경로에서 폼이 비워 보내면 action 이 null 로 변환해 "미상으로 되돌림" 이 된다.
  context_window: z.number().int().positive().optional(),
  max_output_tokens: z.number().int().positive().optional(),
  description: z.string().max(512).optional(),
  display_name: z.string().max(128).optional(),
  // 편집 경로 전용 힌트: 가격 필드가 기존값과 동일하면 false — 불필요한 pricing
  // 버전 생성(effective_from=now)을 건너뛴다. 생성 경로는 항상 미설정(=가격 등록).
  pricing_changed: z.boolean().optional(),
})
  // BEDROCK_RUNTIME_OPENAI 은 endpoint_url 이 **필수**다. 게이트웨이 어댑터가 SigV4 서명
  // 리전을 endpoint 호스트(bedrock-runtime.{region}.amazonaws.com)에서 뽑아내므로, 비어
  // 있으면 서명 자체가 불가능하고 모든 호출이 502 로 죽는다. 등록 시점에 막지 않으면
  // 화면상 정상으로 보이는 모델이 런타임에만 실패한다 — 원인 추적이 가장 어려운 형태다.
  // (Mantle/BEDROCK 계열은 기존대로 optional 이므로 이 refine 은 그들에게 영향이 없다.)
  .refine(
    (d) => d.provider.toUpperCase() !== 'BEDROCK_RUNTIME_OPENAI' || !!d.endpoint_url?.trim(),
    {
      path: ['endpoint_url'],
      message: 'Endpoint URL is required for BEDROCK_RUNTIME_OPENAI (e.g. https://bedrock-runtime.us-east-2.amazonaws.com/openai)',
    },
  )
  // 같은 이유로 host 형태도 검사한다. 오타(bedrock-runtime.us-east-2.amazonaws.com 대신
  // bedrock.us-east-2… 등)는 리전 추출을 실패시켜 ValueError 로 502 가 된다.
  .refine(
    (d) =>
      d.provider.toUpperCase() !== 'BEDROCK_RUNTIME_OPENAI' ||
      /^https:\/\/bedrock-runtime\.[a-z0-9-]+\.amazonaws\.com/i.test(d.endpoint_url?.trim() ?? ''),
    {
      path: ['endpoint_url'],
      message: 'Must be https://bedrock-runtime.{region}.amazonaws.com/openai',
    },
  )
  // provider_model_id 는 cross-region inference profile ID 여야 한다. 접두사 없는
  // openai.gpt-5.6-* 는 inferenceTypesSupported=[INFERENCE_PROFILE] 이라 호출 불가이며,
  // Bedrock 이 400 "The provided model identifier is invalid" 를 돌려준다(실측).
  .refine(
    (d) =>
      d.provider.toUpperCase() !== 'BEDROCK_RUNTIME_OPENAI' ||
      /^(us|global|eu|apac|us-gov)\./.test(d.model_id.trim()),
    {
      path: ['model_id'],
      message: 'Must be a cross-region inference profile ID (e.g. us.openai.gpt-5.6-terra)',
    },
  );

export interface ModelDeactivateForm {
  alias: string;
}

// 즉시 비활성화만 지원(예약/유예 기능 없음). 백엔드 StatusPatchRequest 는 {active: bool} 만 받아
// 즉시 전환한다 — UI 도 이에 맞춘다.
export const ModelDeactivateSchema = z.object({
  alias: z.string().min(1),
});

// ─── Analytics ────────────────────────────────────────────────────────────────

export interface AnalyticsFilterForm {
  period: PeriodType;
  start_date?: string | null; // ISO 8601 date — required when period === 'custom'
  end_date?: string | null; // ISO 8601 date — required when period === 'custom'
  group_by: GroupByType;
  scope?: string | null;
}

export const AnalyticsFilterSchema = z
  .object({
    period: z.enum(['7d', '30d', '90d', 'custom']),
    start_date: z.string().date().nullable().optional(),
    end_date: z.string().date().nullable().optional(),
    group_by: z.enum(['model', 'team', 'user']),
    scope: z.string().nullable().optional(),
  })
  .refine(
    (data) => {
      if (data.period === 'custom') {
        return !!data.start_date && !!data.end_date;
      }
      return true;
    },
    { message: 'start_date and end_date are required for custom period', path: ['start_date'] }
  );

export interface ExportConfig {
  format: 'csv' | 'json';
  filters: AnalyticsFilterForm;
}

export const ExportConfigSchema = z.object({
  format: z.enum(['csv', 'json']),
  filters: AnalyticsFilterSchema,
});

// ─── Organisation ─────────────────────────────────────────────────────────────

// ─── Rate Limits ──────────────────────────────────────────────────────────────

export interface RateLimitSetForm {
  target_id: string;
  scope: RateLimitScope;
  rpm?: number | null;
  tpm?: number | null;
  cpm?: number | null;
  cph?: number | null;
}

export const RateLimitSetSchema = z.object({
  target_id: z.string().min(1),
  scope: z.enum(['USER', 'TEAM', 'GLOBAL']),
  rpm: z.number().int().positive().nullable().optional(),
  tpm: z.number().int().positive().nullable().optional(),
  cpm: z.number().positive().nullable().optional(),
  cph: z.number().positive().nullable().optional(),
});

// ─── Page Permission Map ──────────────────────────────────────────────────────

/**
 * Maps URL pathname patterns to the array of UserRole values that may access them.
 * Evaluated by checkPagePermission in src/lib/auth.ts.
 */
export type PagePermissionMap = Record<string, UserRole[]>;

// ─── Inferred Zod Types ───────────────────────────────────────────────────────

export type BudgetSetInput = z.infer<typeof BudgetSetSchema>;
export type ModelCreateInput = z.infer<typeof ModelCreateSchema>;
export type ModelDeactivateInput = z.infer<typeof ModelDeactivateSchema>;
export type AnalyticsFilterInput = z.infer<typeof AnalyticsFilterSchema>;
export type ExportConfigInput = z.infer<typeof ExportConfigSchema>;
export type RateLimitSetInput = z.infer<typeof RateLimitSetSchema>;
