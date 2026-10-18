// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { z } from 'zod';
import type { GroupByType, PeriodType, UserRole } from './enums';

// ─── Budgets ──────────────────────────────────────────────────────────────────

// 실제 페이지네이션 봉투는 offset/page 가 아니라 커서다 —
// {items, pagination:{cursor,limit,has_more}} (actions/keys.ts KeyListResponse).
// 예전 PaginatedResponse<T>(total/page/…) 타입은 와이어와 다른 형태라 삭제했다.
// APIError 역시 여기 있던 interface 가 아니라 lib/utils/retry 의 class 를 쓴다.

export const BudgetSetSchema = z.object({
  target_id: z.string().min(1, 'Target ID is required'),
  target_type: z.enum(['TEAM', 'USER']),
  max_budget_usd: z.number().nonnegative('Budget must be 0 or greater'),
  policy: z.enum(['HARD_BLOCK', 'SOFT_WARNING', 'THROTTLE']).optional(),
  alert_thresholds: z.array(z.number().int().min(1).max(100)).optional(),
  // TEAM 전용: 기본 유저 cap D — undefined=보존, null=해제(§3-1).
  default_user_cap_usd: z.number().nonnegative().nullish(),
});

// ─── Models ───────────────────────────────────────────────────────────────────

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
  // 장문(>threshold) 구간 단가 — 백엔드 PricingRequest 와 같은 all-or-none
  // 규약: threshold 가 있으면 5개 단가 전부 필요하고, threshold 없이
  // 단가만 오면 조용히 버려지는 대신 거부한다(아래 superRefine).
  // 생략 시 생성=무티어(알려진 모델은 서버가 자동 파생), 편집=기존 티어 유지.
  // Integer 컬럼 상한 — 초과 시 서버 422 대신 INSERT 500 이 나므로 폼에서 막는다.
  long_context_threshold_tokens: z.number().int().positive().max(2147483647).optional(),
  long_context_input_price_per_1k: z.number().nonnegative().optional(),
  long_context_output_price_per_1k: z.number().nonnegative().optional(),
  long_context_cache_creation_5m_price_per_1k: z.number().nonnegative().optional(),
  long_context_cache_creation_1h_price_per_1k: z.number().nonnegative().optional(),
  long_context_cache_read_price_per_1k: z.number().nonnegative().optional(),
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
  )
  // 장문 티어 all-or-none — 백엔드 ModelCreateRequest._long_tier_is_one_unit
  // 와 같은 규약. 결손 단가는 조용히 단락 요율로 과소청구되므로 둘 다 막는다.
  .superRefine((d, ctx) => {
    const priceKeys = [
      'long_context_input_price_per_1k',
      'long_context_output_price_per_1k',
      'long_context_cache_creation_5m_price_per_1k',
      'long_context_cache_creation_1h_price_per_1k',
      'long_context_cache_read_price_per_1k',
    ] as const;
    if (d.long_context_threshold_tokens != null) {
      for (const k of priceKeys) {
        if (d[k] == null) {
          ctx.addIssue({
            code: 'custom',
            path: [k],
            message: 'Required when a long-context threshold is set',
          });
        }
      }
    } else {
      for (const k of priceKeys) {
        if (d[k] != null) {
          ctx.addIssue({
            code: 'custom',
            path: ['long_context_threshold_tokens'],
            message: 'Threshold is required when long-context prices are set',
          });
          break;
        }
      }
    }
  });

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
  // 앱(client) 필터 — 대시보드의 client 필터가 드릴다운 링크로 넘어올 때 보존
  // 한다(R25-N-UI4). 'all'·미지정은 필터 없음.
  client?: string | null;
}

// ─── Rate Limits ──────────────────────────────────────────────────────────────

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

// 스키마에서 파생되는 입력 타입이 필요하면 사용처에서 z.infer<typeof ...Schema>
// 로 둔다 — 미사용 추론 타입을 export 해두면 와이어 계약과 따로 놀며 부패한다.
