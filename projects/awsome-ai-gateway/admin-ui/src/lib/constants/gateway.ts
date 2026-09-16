// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Gateway enums mirrored for the console — clients (apps), providers, api_formats.
 *
 * Single source of truth for the frontend. Deliberately a PLAIN module (no
 * `'use server'`, no `'use client'`) so both server actions and client components can
 * import it: a `'use server'` file may only export async functions, which is why
 * `lib/actions/models.ts` cannot host the provider→api_format table itself.
 *
 * ## Why these lists must be exactly one list each
 *
 * The console had the client set copy-pasted into seven places and the provider set
 * into three. Adding the third client (`codex`) and the fourth provider
 * (`BEDROCK_MANTLE_OPENAI`) required finding every copy, and each miss failed
 * differently — several of them SILENTLY:
 *
 *  - provider `<select>` missing an option → the alias cannot be created at all
 *  - PROVIDER_API_FORMAT missing a key → falls through a `?? 'OPENAI_COMPATIBLE'`
 *    default and persists the WRONG wire format, with no error anywhere
 *  - endpoint-required guard missing a provider → alias saves with endpoint_url=null
 *    and fails later as a 500 at request time instead of at registration time
 *  - dashboard client allowlist missing a value → `?client=codex` silently falls back
 *    to 'all', so whole-fleet numbers render under a filtered URL
 *
 * ## Backend counterparts — these MUST agree
 *
 *  - clients:     admin-api `app/core/clients.py` (VALID_CLIENTS / ANALYTICS_APPS),
 *                 gateway-proxy `services/client_identifier.py` (the strings it
 *                 RETURNS — a client is a routing key, so a typo here means the
 *                 routing profile is never found and the request 404s)
 *  - providers:   admin-api `app/schemas/common.py` ProviderEnum,
 *                 gateway-proxy `app/schemas/domain.py` ProviderType
 *  - api_formats: admin-api `app/schemas/common.py` ApiFormatEnum,
 *                 gateway-proxy `app/schemas/domain.py` ApiFormat
 *  - DB CHECKs:   ck_budget_configs_client / ck_budget_usages_client /
 *                 ck_user_allowed_clients_client (widened by migration 0026)
 */

// ─── Clients (apps) ───────────────────────────────────────────────────────────

/** Grantable, budgetable, routable clients. Order drives UI ordering. */
export const CLIENTS = ['claude-code', 'cowork', 'codex'] as const;
export type GatewayClient = (typeof CLIENTS)[number];

/**
 * Catch-all bucket for unidentified callers. Analytics-only: `usage_logs.client` is
 * NULL for pre-feature rows and admin-api folds those into 'other'
 * (`usage_filters.client_coalesce_expr()`). NOT grantable and NOT budgetable — it has
 * no routing profile, so never add it to CLIENTS.
 */
export const CLIENT_OTHER = 'other';

/** Values accepted by the dashboard `?client=` axis. */
export const CLIENT_FILTER_VALUES = ['all', ...CLIENTS, CLIENT_OTHER] as const;

/** Display labels. Keep in sync with `lib/utils/modelLabel.ts`. */
export const CLIENT_LABELS: Record<string, string> = {
  'claude-code': 'Claude Code',
  cowork: 'Cowork',
  codex: 'Codex',
  other: 'Other',
};

/** Short badge labels for dense tables. */
export const CLIENT_SHORT_LABELS: Record<string, string> = {
  'claude-code': 'CC',
  cowork: 'Cowork',
  codex: 'Codex',
};

export function clientLabel(client: string): string {
  return CLIENT_LABELS[client] ?? client;
}

// ─── 두 개의 allowed_clients 축 ───────────────────────────────────────────────
//
// 이름이 같고 의미가 반대인 컬럼이 둘 있다. 섞어 쓰면 접근제어가 조용히 뒤집히므로
// 아래 두 블록의 헬퍼는 **서로 절대 교차 사용하지 않는다**.
//
//  (A) USER 축 = `auth.user_allowed_clients` (행 존재 모델)
//      행 없음 == `[]` == 제한 없음(전체 앱 허용). 와이어 포맷이 "전부 거부" 를 표현할
//      수 없다. gateway-proxy `router_service.check_client_scope` 가 여전히
//      `if not allowed_clients: return` 이고(★ 문단 참고), 스냅샷 writer 양쪽이 빈
//      목록을 None 으로 접는다(gateway auth_service.py:132, admin-api key_service.py:137).
//      → expandAllowedClients / collapseAllowedClients (바로 아래)
//
//  (B) MODEL 축 = `model.model_aliases.allowed_clients` (3-state)
//      NULL = 제한 없음(앞으로 추가되는 앱까지) / `[]` = 허용 앱 없음(전면 거부) /
//      목록 = 그 앱만. gateway-proxy `router_service.check_client_model_scope` 가
//      `if allowed is None: return` 으로 fail-closed 하게 바뀌었다.
//      → modelAppScope (아래)

/**
 * DB → UI: the EFFECTIVE set of apps a user may call. **USER 축 전용**(A).
 *
 * `[]` means there are no `user_allowed_clients` rows, which the gateway reads as "no
 * restriction" (`router_service.check_client_scope` only denies when the list is
 * non-empty). So an empty list expands to every client — it is NOT "denied everywhere".
 * Values not in CLIENTS are dropped, so a leftover row for a retired app cannot render
 * a phantom checkbox or a budget field for a client the gateway would 404.
 *
 * Also used to pick which per-app budgets a save may touch: a budget row for an
 * un-granted client is inert (the client is 403'd before billing), so we leave it
 * alone rather than clearing it behind the admin's back.
 *
 * ⚠️ `model_aliases.allowed_clients` 에는 절대 쓰지 마라. 그 축에서 `[]` 는 전면 거부이고,
 * 이 함수는 그것을 "전체 앱 허용" 으로 펼친다 — 게이트웨이가 방금 고친 결함과 정확히 같은
 * 오독이다. 모델 축은 `modelAppScope` 를 쓴다.
 */
export function expandAllowedClients(clients: readonly string[]): string[] {
  if (clients.length === 0) return [...CLIENTS];
  return CLIENTS.filter((c) => clients.includes(c));
}

/**
 * UI → DB: collapse a full selection back to `[]`. **USER 축 전용**(A).
 *
 * Storing the explicit full list would freeze the grant: a client added LATER would be
 * absent from it and therefore silently denied for every already-configured user.
 * Round-tripping through `[]` keeps "no restriction" meaning no restriction.
 *
 * ⚠️ An EMPTY selection also collapses to `[]` — i.e. it grants everything, the exact
 * opposite of what the admin clicked. The wire format simply cannot express "deny all"
 * ON THIS AXIS, so callers MUST refuse to submit an empty selection instead of relying
 * on this (OrgDetailPanel.tsx 의 저장 가드, SetBudgetDialog.tsx).
 *
 * ⚠️ "표현할 수 없다" 는 USER 축에 한정된 말이다. MODEL 축(`model_aliases.allowed_clients`)
 * 은 `{}` 로 전면 거부를 **정확히 표현한다** — 두 축을 대칭으로 "정리" 하면 한쪽이 반드시
 * 뒤집힌다. 모델 축은 `modelAppScope` 를 쓴다.
 */
export function collapseAllowedClients(selected: readonly string[]): string[] {
  const kept = CLIENTS.filter((c) => selected.includes(c));
  return kept.length === CLIENTS.length ? [] : kept;
}

/**
 * MODEL 축(B) 의 3-state 판별자 — `model_aliases.allowed_clients` 를 읽는 **모든** 곳이
 * 이 함수 또는 이 함수로 구현된 `modelAllowsClient` 를 지나간다.
 *
 * 소비자 전체(감사할 때 이 목록이 곧 grep 대상이다):
 *   modelAppScope       — 3갈래 표시/편집: ModelsTable.tsx 의 허용 앱 배지,
 *                         CreateModelDialog.tsx 의 prefill, AppPolicyPanel.tsx 의 '전체 허용' 열
 *   modelAllowsClient   — "이 앱이 이 alias 를 쓸 수 있는가" 한 가지 질문:
 *                         AppPolicyPanel.tsx 의 '이 앱 허용' 체크박스,
 *                         WebSearchTogglePanel.tsx 의 미지원 alias 경고
 * 두 화면은 예전에 `=== null` / `.length === 0` / `includes` 를 각자 손으로 썼다. 결과는
 * 맞았지만 판정이 세 군데로 흩어져 있었고, 이 문단이 "전부 여기를 지난다" 고 단정하고 있어서
 * 다음 사람이 modelAppScope 만 grep 하면 그 사본들을 놓치게 되어 있었다.
 *
 * 정본 의미(admin-api `schemas/models.py:61-69`, 강제 지점은 gateway-proxy
 * `services/router_service.py::check_client_model_scope` 의 `if allowed is None: return`):
 *
 *   null/undefined → 'unrestricted' : 제한 없음. 지금 앱 + **앞으로 추가되는 앱**까지 허용.
 *   []             → 'none'         : 허용 앱 없음. 어떤 앱도 이 alias 를 호출할 수 없다.
 *   목록           → 'list'         : 그 앱만.
 *
 * 왜 헬퍼로 뽑는가: 이 필드의 버그는 전부 `!clients` / `clients.length === 0` /
 * `?? []` / `|| null` 같은 falsy 판정 한 줄에서 나왔다(ModelsTable 의 배지가 전면 거부
 * 모델을 "전체" 로 표시했고, 편집 다이얼로그가 NULL 모델을 `[]` 로 저장했다). 판정을 한
 * 군데로 모으면 새 소비자가 같은 실수를 되풀이할 수 없다.
 *
 * `undefined` 를 'unrestricted' 로 접는 이유: 이 필드를 아직 내려주지 않는 구버전
 * admin-api 응답에서 화면이 "전면 거부" 로 보이는 것보다는 낫다(관측된 값이 아니라 필드
 * 부재이므로 새 정책을 발명하지 않는 쪽). admin-api ModelResponse 는 항상 키를 포함한다.
 */
export type ModelAppScope =
  | { kind: 'unrestricted' }
  | { kind: 'none' }
  | { kind: 'list'; clients: string[] };

export function modelAppScope(allowed: readonly string[] | null | undefined): ModelAppScope {
  if (allowed === null || allowed === undefined) return { kind: 'unrestricted' };
  if (allowed.length === 0) return { kind: 'none' };
  return { kind: 'list', clients: [...allowed] };
}

/**
 * "이 client 가 이 alias 를 호출할 수 있는가" — MODEL 축(B) 전용.
 *
 * gateway-proxy `router_service.py::check_client_model_scope` 와 admin-api 의 조회 SQL
 * (`allowed_clients IS NULL OR :client = ANY(allowed_clients)`, `routers/apps.py`) 과 **같은**
 * 판정이다. `[]` 는 어느 client 도 통과하지 못한다(전면 거부).
 *
 * 호출부에서 `!allowed || allowed.includes(c)` 로 줄이면 `[]` 가 전체 허용으로 뒤집힌다 —
 * 게이트웨이가 방금 고친 결함과 같은 falsy 오독이라 헬퍼로 못 박는다.
 */
export function modelAllowsClient(
  allowed: readonly string[] | null | undefined,
  client: string
): boolean {
  const scope = modelAppScope(allowed);
  if (scope.kind === 'unrestricted') return true;
  if (scope.kind === 'none') return false;
  return scope.clients.includes(client);
}

// ─── Providers ────────────────────────────────────────────────────────────────

export const PROVIDERS = [
  'BEDROCK',
  'BEDROCK_MANTLE',
  'BEDROCK_MANTLE_OPENAI',
  // Codex 의 두 번째 엔드포인트. BEDROCK_MANTLE_OPENAI 와 와이어 포맷은 동일하고
  // (둘 다 OPENAI_RESPONSES) 호스트와 인증만 다르다: mantle 은 bearer 토큰,
  // 여기는 SigV4(service=bedrock). migration 0032.
  'BEDROCK_RUNTIME_OPENAI',
  'OPENMODEL',
] as const;
export type Provider = (typeof PROVIDERS)[number];

/**
 * provider → api_format. admin-api performs NO provider↔api_format cross-validation
 * (`model_service` just coerces the enum), so an unmapped provider is persisted with
 * whatever this table yields — a wrong value here is silent, permanent, and only shows
 * up as mis-routed traffic later.
 *
 * OPENAI_RESPONSES is what makes Codex work: gateway-proxy's `/v1/responses` route resolves
 * the model with `resolve_responses_model`, which accepts **either** Responses provider
 * (`router_service._RESPONSES_PROVIDERS` = {BEDROCK_MANTLE_OPENAI, BEDROCK_RUNTIME_OPENAI}).
 * It is deliberately NOT the older `resolve_codex_model`, which pins BEDROCK_MANTLE_OPENAI
 * alone — that pin is INV-2 (a runtime alias resolves nowhere, so the route falls back to the
 * profile default and silently serves a mantle model under a `codex-rt-*` name).
 */
export const PROVIDER_API_FORMAT: Record<string, string> = {
  BEDROCK: 'BEDROCK_NATIVE',
  BEDROCK_MANTLE: 'ANTHROPIC_MESSAGES',
  BEDROCK_MANTLE_OPENAI: 'OPENAI_RESPONSES',
  // mantle 과 같은 OPENAI_RESPONSES 다. 2026-08-31 실측으로 요청/응답 스키마가
  // 동일함을 확인했다(docs/bedrock-runtime-op0-live-findings-20260831.md §3).
  BEDROCK_RUNTIME_OPENAI: 'OPENAI_RESPONSES',
  OPENMODEL: 'OPENAI_COMPATIBLE',
};

/**
 * Providers whose adapter CONCATENATES the stored endpoint
 * (`f"{endpoint.rstrip('/')}/v1/..."`). `router_service` maps a NULL endpoint_url to
 * `""`, not to a default, so the adapter would request a relative URL and httpx
 * raises — hence endpoint_url is required, not optional, for these.
 */
export const ENDPOINT_REQUIRED_PROVIDERS: readonly string[] = [
  'BEDROCK_MANTLE',
  'BEDROCK_MANTLE_OPENAI',
  // runtime 어댑터도 저장된 endpoint 를 이어붙여 호출하고, 무엇보다 호스트의 리전이
  // SigV4 서명 리전을 결정한다(routing_profiles.region 이 아니다 — codex 프로필은
  // us-east-2 로 고정되어 있어서 ap-northeast-2 별칭을 표현할 수 없다). 빈 값이면
  // 상대 URL 이 되어 httpx 가 즉시 예외를 던진다.
  'BEDROCK_RUNTIME_OPENAI',
];

export function requiresEndpoint(provider: string): boolean {
  return ENDPOINT_REQUIRED_PROVIDERS.includes(provider);
}

/**
 * Prefilled endpoint suggestions. Both the region AND the trailing path segment matter:
 *
 *  - the path selects the wire dialect (`/anthropic` = Messages, `/openai` = Responses)
 *  - the region must match `routing_profiles.region`, because the bearer token is
 *    SigV4-signed with the profile's region while the host comes from this URL. A
 *    mismatch is a 401, not a redirect.
 *  - the region must also be covered by the gateway's IRSA `bedrock-mantle:*` grant,
 *    or every call is AccessDenied.
 *
 * These are editable defaults, not constraints — confirm the region against the
 * routing profile and the IRSA policy before registering an alias.
 */
/**
 * 이 provider 의 어댑터가 저장된 endpoint 뒤에 `/v1/responses` 를 붙이는가.
 *
 * 하드코딩된 `provider === 'BEDROCK_MANTLE_OPENAI'` 비교를 대체한다. 그 비교는
 * BEDROCK_RUNTIME_OPENAI 가 추가된 순간 조용히 틀린 안내문(`/v1/messages 가 자동으로
 * 추가됩니다`)을 보여줬다 — 화면은 정상으로 보이고 운영자만 잘못된 URL 을 만든다.
 * api_format 테이블을 단일 근거로 삼으므로 provider 를 또 추가해도 여기서 갈라진다.
 */
export function appendsResponsesPath(provider: string): boolean {
  return PROVIDER_API_FORMAT[provider] === 'OPENAI_RESPONSES';
}

/**
 * SigV4 로 서명하는 provider — 즉 endpoint host 의 리전이 **서명 리전**인 provider.
 *
 * mantle 과 runtime 의 결정적 차이다. mantle 은 bearer 토큰을 쓰고 리전은
 * `routing_profiles.region` 에서 오지만, runtime 은 host 에서 파싱한 리전으로 SigV4 를
 * 서명한다. 두 경우의 실패 모드가 달라서(전자는 401, 후자는 서명 리전 불일치) 안내
 * 문구를 반드시 갈라 써야 한다.
 */
export function signsWithSigV4(provider: string): boolean {
  return provider === 'BEDROCK_RUNTIME_OPENAI';
}

/**
 * 서버사이드 웹서치 루프를 돌릴 수 있는 provider — gateway-proxy
 * `providers/web_search_capability.py` 의 `_SERVER_SIDE_WEB_SEARCH_PROVIDERS` 미러.
 *
 * ⚠️ 이 표는 **안내용일 뿐 강제력이 없다.** 실제 게이트는 게이트웨이의 요청 경로 한 곳뿐이고
 * (`routers/openai_compat.py`, `routers/messages.py`), 콘솔이 막을 수 있는 성질의 규칙이
 * 아니다: `web_search_enabled` 는 `model.routing_profiles` 의 client 단위 플래그인데
 * 엔드포인트는 `model_aliases` 의 alias 단위 값이고, codex 의 두 트랙이 `client='codex'`
 * 행 하나를 공유한다. client→alias 는 다대다라서 저장 시점에는 증명할 수 없다. 그래서 UI 는
 * "이 앱의 트래픽 중 일부는 검색 없이 처리된다" 를 **알려주는** 역할만 한다.
 *
 * ⚠️ 문구를 쓸 때: "AWS 가 bedrock-runtime 에서 웹서치를 막는다" 는 **거짓이다**. 이 루프는
 * AgentCore Gateway 의 관리형 WebSearch 커넥터를 게이트웨이가 대신 호출하는 에뮬레이션이라
 * (`bedrock-websearch:*` 를 부르지 않는다) AWS 가 거부할 대상이 없다. 실제 근거 3개는
 * `web_search_capability.py` 의 docstring 에 있다. 요약: (1) 이 트랙에 대한 고객의 확정
 * 방침(SCP Deny), (2) 트랙의 유일한 목적인 native body invocation log 가 클라이언트가 보내지
 * 않은 본문을 기록하게 되어 감사 로그가 위조되는 셈이 됨, (3) 프롬프트 1건이 최대 5회
 * 모델 호출로 늘어나 프롬프트 단위 귀속이 깨짐.
 *
 * ALLOWLIST 다. 새 provider 는 검토 후 **명시적으로 추가**해야 하고, 목록에 없으면 무조건
 * 불가로 취급한다(게이트웨이와 같은 fail-closed).
 */
export const WEB_SEARCH_CAPABLE_PROVIDERS: readonly string[] = [
  // Claude Code / Cowork, plain Bedrock InvokeModel. 게이트웨이 게이트에 provider 조건이
  // 없던 시절부터 이미 루프를 돌고 있었다 — 빼면 "조용히 꺼짐" 회귀가 된다.
  'BEDROCK',
  'BEDROCK_MANTLE',
  'BEDROCK_MANTLE_OPENAI',
  // BEDROCK_RUNTIME_OPENAI 는 없다(위 근거 3개). OPENMODEL 도 없다 — 어느 게이트에서도
  // 도달하지 않으며, 미검토 provider 는 fail-closed 가 원칙이다.
];

export function supportsServerSideWebSearch(provider: string | null | undefined): boolean {
  // null/undefined/미등록 문자열은 모두 false. 부분적으로 채워진 모델 목록을 읽는 호출자가
  // "검색 안 됨" 을 받는 건 안전하지만, "아무도 검토하지 않은 엔드포인트에서 검색됨" 은 아니다.
  return provider ? WEB_SEARCH_CAPABLE_PROVIDERS.includes(provider) : false;
}

export const PROVIDER_DEFAULT_ENDPOINT: Record<string, string> = {
  BEDROCK_MANTLE: 'https://bedrock-mantle.ap-northeast-1.api.aws/anthropic',
  BEDROCK_MANTLE_OPENAI: 'https://bedrock-mantle.us-east-2.api.aws/openai',
  // ⚠️ mantle 과 호스트 규칙이 다르다: `bedrock-mantle.<region>.api.aws` 가 아니라
  // `bedrock-runtime.<region>.amazonaws.com` 이다. 경로는 `/openai` 로 같다.
  // us-east-2 는 `us.` / `global.` CRIS 6종 모두, ap-northeast-2 는 `global.` 3종만
  // 제공한다(2026-08-31 list-inference-profiles 실측). 리전을 바꾸면 IRSA 의
  // inference-profile ARN 목록도 같이 넓혀야 한다 — 아니면 전부 AccessDenied.
  BEDROCK_RUNTIME_OPENAI: 'https://bedrock-runtime.us-east-2.amazonaws.com/openai',
};
