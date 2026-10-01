// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { cookies } from 'next/headers';
import { getTranslations } from 'next-intl/server';
import { adminAPI } from '@/lib/api-client';
import { normalizeAllocation } from '@/lib/budget-allocation';
import { currentCalendarMonth } from '@/lib/utils/period';
import { parseJWT } from '@/lib/auth';
import type { BudgetSummaryItem, ModelListItem, TeamBudgetAllocation } from '@/types/entities';
import { BudgetSummaryTable } from '@/components/budgets/BudgetSummaryTable';
import { TeamAllocationView } from '@/components/budgets/TeamAllocationView';
import { ErrorState } from '@/components/common/ErrorState';
import { RegisterScreenContext } from '@/components/chat/RegisterScreenContext';

interface BudgetsPageProps {
  // /users 패널의 "예산에서 편집" 딥링크 — ?team= 은 팀 행 펼침·스크롤,
  // ?user= 는 해당 유저의 예산 다이얼로그 자동 오픈(BudgetSummaryTable이 처리).
  // searchParams 는 sync 객체다(await 금지 — 다른 페이지와 같은 패턴).
  searchParams: { team?: string; user?: string };
}

const UUID_RE = /^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$/;

export default async function BudgetsPage({ searchParams }: BudgetsPageProps) {
  const t = await getTranslations('budgets');
  const focusTeam = searchParams.team && UUID_RE.test(searchParams.team) ? searchParams.team : undefined;
  const focusUser = searchParams.user && UUID_RE.test(searchParams.user) ? searchParams.user : undefined;
  const cookieStore = cookies();
  const jwt = cookieStore.get('admin_jwt')?.value;
  const session = jwt ? parseJWT(jwt) : null;
  const isAdmin = session?.role === 'ADMIN';

  // 리포팅 TZ 기준 현재월 — 로컬 new Date()(pod UTC)로 계산하면 매월 1일 리포팅 TZ
  // 새벽(UTC 자정~TZ 자정)에 지난달 요약을 보여준다. 백엔드 집계 버킷과 같은 env 값.
  const period = currentCalendarMonth();

  interface RawBudgetItem {
    target_type: string;
    target_id: string;
    target_name: string | null;
    team_id?: string | null;
    is_active?: boolean;
    limit_usd: string | null;
    used_usd: string;
    remaining_usd: string | null;
    usage_pct: string | null;
    default_user_cap_usd?: string | null;
    cap_source?: 'individual' | 'team_default' | null;
    downgrade_rule_count?: number | null;
    downgrade_enabled?: boolean | null;
  }

  // 조회 실패를 "예산 없음"과 구분 — 실패 시 재시도 가능한 에러 상태를 렌더한다.
  const summaryResult = await adminAPI
    .get<{ summary: RawBudgetItem[] }>('/admin/budgets/summary', { period })
    .then((v) => ({ ok: true as const, value: v }))
    .catch(() => ({ ok: false as const }));

  const rawItems = summaryResult.ok
    ? Array.isArray(summaryResult.value)
      ? summaryResult.value
      : summaryResult.value.summary ?? []
    : [];

  const items: BudgetSummaryItem[] = rawItems.map((r) => {
    const pct = r.usage_pct != null ? parseFloat(r.usage_pct) || 0 : null;
    return {
      target_id: r.target_id,
      target_type: r.target_type.toUpperCase() as BudgetSummaryItem['target_type'],
      target_name: r.target_name ?? r.target_id,
      team_id: r.team_id ?? null,
      is_active: r.is_active ?? true,
      limit: r.limit_usd != null ? parseFloat(r.limit_usd) || 0 : null,
      used: parseFloat(r.used_usd) || 0,
      remaining: r.remaining_usd != null ? parseFloat(r.remaining_usd) || 0 : null,
      usage_pct: pct,
      alert_level: pct != null ? (pct >= 100 ? 'CRITICAL' : pct >= 80 ? 'WARNING' : 'NORMAL') : 'NORMAL',
      default_user_cap_usd:
        r.default_user_cap_usd != null ? parseFloat(r.default_user_cap_usd) : null,
      cap_source: r.cap_source ?? null,
      downgrade_rule_count: r.downgrade_rule_count ?? null,
      downgrade_enabled: r.downgrade_enabled ?? null,
    } as BudgetSummaryItem;
  });

  let teamAllocation: TeamBudgetAllocation | null = null;
  if (!isAdmin && session?.team_id) {
    teamAllocation = await adminAPI
      .get(`/admin/budgets/team/${session.team_id}/allocation`)
      .then(normalizeAllocation)
      .catch(() => null);
  }

  interface APIModelItem {
    alias: string;
    provider: string;
    provider_model_id: string;
    endpoint_url: string | null;
    status: string;
    description: string | null;
    display_name: string | null;
    current_pricing: {
      input_price_per_1k_tokens: string;
      output_price_per_1k_tokens: string;
      cache_creation_5m_price_per_1k_tokens?: string;
      cache_creation_1h_price_per_1k_tokens?: string;
      cache_read_price_per_1k_tokens?: string;
    } | null;
    context_window: number | null;
    max_output_tokens: number | null;
  }

  const modelsRes = await adminAPI
    .get<{ items: APIModelItem[] }>('/admin/models')
    .catch(() => ({ items: [] as APIModelItem[] }));
  const models: ModelListItem[] = (modelsRes.items ?? []).map(m => {
    const p = m.current_pricing;
    return {
      alias: m.alias,
      provider: m.provider,
      model_id: m.provider_model_id,
      endpoint_url: m.endpoint_url ?? null,
      is_active: m.status === 'ACTIVE',
      input_price_per_1k: p ? parseFloat(p.input_price_per_1k_tokens) : 0,
      output_price_per_1k: p ? parseFloat(p.output_price_per_1k_tokens) : 0,
      cache_creation_5m_price_per_1k: p?.cache_creation_5m_price_per_1k_tokens
        ? parseFloat(p.cache_creation_5m_price_per_1k_tokens)
        : 0,
      cache_creation_1h_price_per_1k: p?.cache_creation_1h_price_per_1k_tokens
        ? parseFloat(p.cache_creation_1h_price_per_1k_tokens)
        : 0,
      cache_read_price_per_1k: p?.cache_read_price_per_1k_tokens
        ? parseFloat(p.cache_read_price_per_1k_tokens)
        : 0,
      max_tokens: m.max_output_tokens ?? 0,
      context_window: m.context_window ?? 0,
      description: m.description,
      display_name: m.display_name,
    };
  });

  const teamItems = items.filter(i => i.target_type === 'TEAM');

  // 퀵챗 화면 컨텍스트용 집계 — 개별 target_name(개인 예산은 사람 이름일 수 있음)은
  // 절대 동봉하지 않고, 건수/합계/경보 레벨 분포만. PII 없음.
  const userItems = items.filter(i => i.target_type === 'USER');
  const totalLimit = items.reduce((acc, b) => acc + (b.limit ?? 0), 0);
  const totalUsed = items.reduce((acc, b) => acc + b.used, 0);
  const alertCounts = items.reduce(
    (acc, b) => {
      acc[b.alert_level] = (acc[b.alert_level] ?? 0) + 1;
      return acc;
    },
    {} as Record<string, number>,
  );
  const contextData = isAdmin
    ? {
        팀예산수: teamItems.length,
        개인예산수: userItems.length,
        총한도USD: totalLimit,
        총사용USD: totalUsed,
        경보_위험: alertCounts.CRITICAL ?? 0,
        경보_경고: alertCounts.WARNING ?? 0,
        경보_정상: alertCounts.NORMAL ?? 0,
      }
    : { 뷰: '내 팀 예산 배정' };

  return (
    <div className="space-y-6">
      {/* 퀵챗 화면 컨텍스트 등록(렌더 null) — 예산 집계만(건수/합계/경보 분포).
          개별 사용자·팀 이름은 미동봉(PII). agent 는 query_db 로 상세 재조회 가능. */}
      <RegisterScreenContext page="예산 관리" period={`${period} 월간`} data={contextData} />

      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t('title')}</h1>
      </div>

      {isAdmin ? (
        summaryResult.ok ? (
          <BudgetSummaryTable
            items={items}
            isAdmin={isAdmin}
            models={models}
            currentUserId={session?.user_id}
            focusTeam={focusTeam}
            focusUser={focusUser}
          />
        ) : (
          <ErrorState />
        )
      ) : session?.team_id ? (
        <TeamAllocationView
          teamId={session.team_id}
          initialAllocation={teamAllocation}
          isAdmin={false}
          currentUserId={session?.user_id}
        />
      ) : (
        <p className="text-sm text-muted-foreground">{t('noTeamAssigned')}</p>
      )}
    </div>
  );
}
