'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useState, useEffect, useRef, useTransition } from 'react';
import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import type { OrgTreeNode, ModelListItem, EffectivePolicy } from '@/types/entities';
import {
  forceReauthTeamAction,
  getUserAllowedClientsAction,
  setUserAllowedClientsAction,
  getEffectivePolicyAction,
  getUserAllowedModelsAction,
  setUserAllowedModelsAction,
  setTeamLeaderAction,
  unsetTeamLeaderAction,
} from '@/lib/actions/users';
import { listActiveModelsAction } from '@/lib/actions/models';
import { CLIENTS, type GatewayClient } from '@/lib/constants/gateway';
import { AppDialog } from '@/components/common/AppDialog';
import { PolicySection } from '@/components/common/PolicySection';
import { EMPTY_POLICY_SUMMARY, type PolicySummary } from '@/components/common/policySummary';
import { UnsavedApplyBar } from '@/components/common/UnsavedApplyBar';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import { useToast } from '@/components/common/ToastProvider';
import { Badge, type BadgeTone } from '@/components/common/Badge';
import { TeamModelPermissionPanel, type TeamModelPermissionHandle } from '@/components/models/TeamModelPermissionPanel';
import { ScopeRateLimitPanel, type ScopeRateLimitHandle } from '@/components/users/ScopeRateLimitPanel';
import { ScopeAppAccessPanel, type ScopeAppAccessHandle } from '@/components/users/ScopeAppAccessPanel';
import { EffectivePolicyCard } from '@/components/users/EffectivePolicyCard';
import { BudgetGaugeRow } from '@/components/budgets/budgetVisuals';

interface OrgDetailPanelProps {
  node: OrgTreeNode | null;
  /** 패널 내 미저장 편집 여부를 부모(OrgTreeView 의 노드 전환 가드)에 보고한다. */
  onDirtyChange?: (_dirty: boolean) => void;
  /** 트리 root(ORGANIZATION) 의 id — 팀 패널이 조직 정책 상속 표시에 쓴다. */
  orgId?: string;
  /** 저장 성공 후 트리 재조회 콜백 — 개별설정 점이 stale 해지지 않게 한다. */
  onSaved?: () => void;
}

// Role labels are now i18n-driven — see t('roleLabel.ADMIN') etc.

const ROLE_TONE: Record<string, BadgeTone> = {
  ADMIN: 'pink',
  TEAM_LEADER: 'sky',
  DEVELOPER: 'teal',
};

export function OrgDetailPanel({ node, onDirtyChange, orgId, onSaved }: OrgDetailPanelProps) {
  const t = useTranslations('users');

  if (!node) {
    return (
      <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
        {t('selectNodePrompt')}
      </div>
    );
  }

  // ── ORGANIZATION ────────────────────────────────────────────────────────────
  if (node.type === 'ORGANIZATION') {
    const deptCount = node.children?.length ?? 0;
    const orgTeamCount = node.meta.team_count ?? null;
    const orgMemberCount = node.meta.member_count ?? null;
    return (
      <div>
        <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
        <div className="flex items-center gap-2 text-sm mb-2">
          <span className="text-muted-foreground">{t('departmentCount')}</span>
          <span className="font-medium">{t('countSuffix', { count: deptCount })}</span>
        </div>
        {orgTeamCount !== null && (
          <div className="flex items-center gap-2 text-sm mb-2">
            <span className="text-muted-foreground">{t('teamCount')}</span>
            <span className="font-medium">{t('countSuffix', { count: orgTeamCount })}</span>
          </div>
        )}
        {orgMemberCount !== null && (
          <div className="flex items-center gap-2 text-sm mb-2">
            <span className="text-muted-foreground">{t('memberCount')}</span>
            <span className="font-medium">{t('memberCountValue', { count: orgMemberCount })}</span>
          </div>
        )}
        <div className="mt-4">
          <ScopeAppAccessPanel scope="organization" scopeId={node.id} onDirtyChange={onDirtyChange} />
        </div>
      </div>
    );
  }

  // ── DEPARTMENT ──────────────────────────────────────────────────────────────
  if (node.type === 'DEPARTMENT') {
    // ⚠️ 예전엔 `node.meta.member_count ?? children.length` 를 **팀 수**로 표시했다.
    //    서버는 그 필드에 하위 팀들의 사용자 수 합을 넣으므로, 20팀×50명 부서가
    //    화면에 "팀 1000개" 로 떴다. 지금은 서버가 team_count 를 따로 준다
    //    (admin-api schemas/users.py OrgNodeMeta).
    //
    //    폴백은 children.length 로만 둔다 — 팀 수를 모르면 "모른다" 가 맞고,
    //    사람 수로 대신 채우면 정확히 그 버그가 재발한다.
    const teamCount = node.meta.team_count ?? node.children?.length ?? 0;
    const deptMemberCount = node.meta.member_count ?? null;
    return (
      <div>
        <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
        <div className="flex items-center gap-2 text-sm mb-2">
          <span className="text-muted-foreground">{t('teamCount')}</span>
          <span className="font-medium">{t('countSuffix', { count: teamCount })}</span>
        </div>
        {deptMemberCount !== null && (
          <div className="flex items-center gap-2 text-sm mb-2">
            <span className="text-muted-foreground">{t('memberCount')}</span>
            <span className="font-medium">{t('memberCountValue', { count: deptMemberCount })}</span>
          </div>
        )}
      </div>
    );
  }

  // ── TEAM ────────────────────────────────────────────────────────────────────
  if (node.type === 'TEAM') {
    // key=node.id — 팀 A→B 전환 시 같은 컴포넌트 재사용으로 리더 선택·다이얼로그·
    // dirty·로드 상태가 새 팀으로 새는 것을 막는다.
    return <TeamPanel key={node.id} node={node} onDirtyChange={onDirtyChange} orgId={orgId} />;
  }

  // ── USER ────────────────────────────────────────────────────────────────────
  if (node.type === 'USER') {
    return <UserPanel key={node.id} node={node} onDirtyChange={onDirtyChange} onSaved={onSaved} />;
  }

  return null;
}

// ── USER 상세 (앱 접근 권한 토글 포함) ────────────────────────────────────────

// 앱(client) 집합 — gateway.ts CLIENTS 단일 소스 사용. 새 앱 추가 시 거기만 늘리면
// 토글·예산 입력·dirty 비교가 모두 자동으로 확장된다(이전 both/single 이분법 폐기).
const ALL_CLIENTS = CLIENTS;
type ClientId = GatewayClient;

const CLIENT_OPTIONS: Array<{ value: ClientId; label: string }> = [
  { value: 'claude-code', label: 'Claude Code' },
  { value: 'cowork', label: 'Cowork' },
  { value: 'codex', label: 'Codex' },
];

// API allowed_clients([] = 이 레벨 정책 없음 = 상속) → UI 체크 상태.
function clientsToSelected(clients: string[]): ClientId[] {
  if (clients.length === 0) return [...ALL_CLIENTS];
  return ALL_CLIENTS.filter((c) => clients.includes(c));
}

// UI 체크 상태 → API allowed_clients (Option A — 체크한 목록을 그대로 저장).
//   · 빈 선택   → [] = 개인 정책 해제 → 팀/조직 정책 상속 (전면 거부 아님)
//   · 부분 선택 → 명시 화이트리스트
//   · 전체 선택 → 명시 [3개] 목록 — 상속을 끊는 "이 유저는 전부 허용" override.
//     ([] 로내면 상속이 되어 팀 제한이 그대로 적용되던 기존 버그를 막는다)
function selectedToClients(selected: string[]): string[] {
  return ALL_CLIENTS.filter((c) => selected.includes(c));
}

// allowed_clients 비교용 canonical key — 저장값 기준으로 비교(0개=[]=상속과
// 전체=명시목록은 다르다).
function clientsKey(clients: string[]): string {
  return selectedToClients(clients).slice().sort().join(',');
}

// EffectivePolicy 의 *_source 값 → policyState 출처 배지 키.
const SOURCE_KEY = {
  user: 'sourceOwn',
  team: 'sourceTeam',
  organization: 'sourceOrganization',
  none: 'sourceDefault',
} as const;

function UserPanel({
  node,
  onDirtyChange,
  onSaved,
}: {
  node: OrgTreeNode;
  onDirtyChange?: (_dirty: boolean) => void;
  onSaved?: () => void;
}) {
  const t = useTranslations('users');
  const tp = useTranslations('policyState');
  const { toast } = useToast();
  // 로드용/저장용 transition 분리 — 초기 조회 중에 Apply 버튼이 스피너로 보이는 혼동 방지.
  const [isLoadPending, startLoadTransition] = useTransition();
  const [isSavePending, startSaveTransition] = useTransition();

  const email = node.meta.email ?? '-';
  const role = node.meta.role ?? null;
  const teamName = node.meta.team_name ?? t('teamUnassigned');

  const [loadedSelected, setLoadedSelected] = useState<ClientId[]>([...ALL_CLIENTS]);
  const [selected, setSelected] = useState<ClientId[]>([...ALL_CLIENTS]);
  // 허용 클라이언트 정책이 정상 로드됐는지 추적. 로드 실패 시 stale
  // 전체허용([])으로 저장돼 의도치 않게 허용되는 사고를 막기 위해 저장을 건너뛴다.
  const [clientsLoaded, setClientsLoaded] = useState(false);

  // 유효 정책 합성 결과 — 앱별 예산/모델 정책 출처 표시와 EffectivePolicyCard 가
  // 같은 데이터를 쓰므로 한 번만 가져와 공유한다.
  const [policy, setPolicy] = useState<EffectivePolicy | null>(null);

  const toggleClient = (c: ClientId) => {
    setSelected((prev) => (prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c]));
  };

  // 사용자별 허용 모델 (팀 정책 override). 비어있음 = override 해제 → 팀 정책 fallback.
  const [models, setModels] = useState<ModelListItem[]>([]);
  const [loadedModelAliases, setLoadedModelAliases] = useState<string[]>([]);
  const [selectedModelAliases, setSelectedModelAliases] = useState<string[]>([]);
  // ★ 모델 정책이 정상 로드됐는지 추적. 로드 실패 시 stale 빈 목록을
  //   저장하면 기존 override 가 의도치 않게 DELETE(팀 폴백)되어 제한이 풀린다 —
  //   국가핵심기술 제한이므로 로드 실패 시에는 모델 정책 저장 자체를 건너뛴다.
  const [modelsLoaded, setModelsLoaded] = useState(false);
  // 저장된 정책의 표시용 기준선 — override 가 없으면 상속 목록(팀/전체)으로
  // 채워진 체크 상태가 기준이다. 되돌리기·저장 후 표시에 쓴다.
  // dirty 판정은 touched 플래그가 아니라 baseline 과의 비교로 한다 —
  // 토글했다 원상태로 되돌리면 dirty 가 아니고, 그런 상태를 저장하면
  // 상속값이 개인 override 로 굳어 이후 팀 변경이 안 따라온다.
  const [modelBaseline, setModelBaseline] = useState<string[]>([]);
  // 통합 Apply 의 실패 섹션 — 헤더에 "저장 실패" 배지 + 자동 펼침에 쓴다.
  const [failedSection, setFailedSection] = useState<'apps' | 'models' | 'ratelimit' | null>(null);
  // Rate limit 섹션 — 앱/모델과 같은 ref+save 패턴으로 통합 Apply에 합류한다.
  const rateLimitRef = useRef<ScopeRateLimitHandle>(null);
  const [rlDirty, setRlDirty] = useState(false);
  const [rlSummary, setRlSummary] = useState<PolicySummary>(EMPTY_POLICY_SUMMARY);
  // 개인 앱 정책 행 존재 여부 — policy 로드 실패 시 배지 폴백에서 "상속"을
  // 구분하는 데 쓴다(행 없음 = 상속, 있음 = 자체 정책).
  const [hasOwnAppPolicy, setHasOwnAppPolicy] = useState(false);
  // 언마운트 후 setState 방지 — 저장 후 유효정책 재조회는 로드 effect의
  // cancelled 가드 밖에서 돌기 때문이다.
  const mountedRef = useRef(true);
  useEffect(() => () => {
    mountedRef.current = false;
  }, []);

  useEffect(() => {
    // 사용자 전환 시 이전 사용자 상태 잔존 방지(잘못된 저장 차단).
    let cancelled = false;
    setModelsLoaded(false);
    setClientsLoaded(false);
    setLoadedModelAliases([]);
    setSelectedModelAliases([]);
    setHasOwnAppPolicy(false);
    setFailedSection(null);
    startLoadTransition(async () => {
      const [r, p, m, cat] = await Promise.all([
        getUserAllowedClientsAction(node.id),
        getEffectivePolicyAction(node.id),
        getUserAllowedModelsAction(node.id),
        listActiveModelsAction(),
      ]);
      // 빠른 A→B→A 전환에서 늦게 돌아온 응답이 새 노드 상태를 덮지 않게 한다.
      if (cancelled) return;
      if (r.success) {
        setHasOwnAppPolicy(r.data.clients.length > 0);
        const sel = clientsToSelected(r.data.clients);
        // 개인 정책 행이 없으면 상속된 유효 목록(팀/조직 정책)을 프리필해 보여준다 —
        // userModels 와 같은 규칙: 빈 전체체크는 "제한 없음"으로 오독되므로 실제
        // 적용값을 표시한다. loadedSelected 도 같은 값으로 둬야 프리필이 dirty 로
        // 보이지 않고, 그대로 저장되는 일(= 상속값이 개인 override 로 굳음)도 없다.
        const sel2 =
          r.data.clients.length === 0 &&
          p.success &&
          p.data.allowed_clients_source !== 'user' &&
          p.data.allowed_clients
            ? clientsToSelected(p.data.allowed_clients)
            : sel;
        setLoadedSelected(sel2);
        setSelected(sel2);
        setClientsLoaded(true);
      } else {
        toast({
          type: 'error',
          message: t('loadErrors.appAccess'),
          auto_dismiss_ms: 5000,
        });
      }
      if (p.success) {
        setPolicy(p.data);
      } else {
        setPolicy(null);
      }
      if (cat.success) {
        setModels(cat.data);
      } else {
        toast({
          type: 'error',
          message: t('loadErrors.models'),
          auto_dismiss_ms: 5000,
        });
      }
      if (m.success) {
        setLoadedModelAliases(m.data.modelAliases);
        // override 없음 → 유효 목록(팀 정책 또는 전체 모델)을 체크 상태로 표시.
        const display =
          m.data.modelAliases.length > 0
            ? m.data.modelAliases
            : p.success && p.data.allowed_models_source !== 'user'
              ? (p.data.allowed_models ??
                (cat.success ? cat.data.map((mm) => mm.alias) : []))
              : cat.success
                ? cat.data.map((mm) => mm.alias)
                : [];
        setSelectedModelAliases(display);
        setModelBaseline(display);
        setModelsLoaded(true);
      } else {
        toast({
          type: 'error',
          message: t('loadErrors.userModels'),
          auto_dismiss_ms: 5000,
        });
      }
    });
    return () => {
      cancelled = true;
    };
  }, [node.id, t, toast]);

  const toggleModel = (alias: string) => {
    setSelectedModelAliases((prev) =>
      prev.includes(alias) ? prev.filter((a) => a !== alias) : [...prev, alias]
    );
  };

  const handleApply = () => {
    startSaveTransition(async () => {
      setFailedSection(null);
      // 1) 접근 권한 — 실제로 바뀐 경우만 저장. 상속 프리필을 그대로 저장하면
      //    팀/조직 정책이 개인 override 로 굳어 이후 상위 변경이 안 따라온다.
      //    accessDirty 는 clientsLoaded 를 내포하므로 별도 게이트 불필요 —
      //    앱 정책 로드 실패가 모델 저장까지 막지 않게 한다.
      let savedClients: string[] | null = null;
      if (accessDirty) {
        const r = await setUserAllowedClientsAction(node.id, selectedToClients(selected));
        if (!mountedRef.current) return;
        if (!r.success) {
          setFailedSection('apps');
          toast({ type: 'error', message: r.error, auto_dismiss_ms: 4000 });
          return;
        }
        savedClients = r.data.clients;
      }

      // 2) 사용자별 허용 모델 저장 — 빈 배열이면 action 이 DELETE(override 해제)로 처리.
      // ★ 모델 정책이 정상 로드되지 않았으면(modelsLoaded=false) stale 빈 목록을
      //   저장해 기존 override 를 의도치 않게 해제하는 사고를 막기 위해 저장을 건너뛴다.
      let savedAliases: string[] | null = null;
      if (modelsDirty) {
        const mr = await setUserAllowedModelsAction(node.id, selectedModelAliases);
        if (!mountedRef.current) return;
        if (!mr.success) {
          // 접근 권한은 이미 저장됐을 수 있다 — 출처 배지가 stale 해지지 않게
          // 유효 정책을 재조회하고 앱 섹션 표시도 저장 결과로 동기화한다.
          const p3 = await getEffectivePolicyAction(node.id);
          if (!mountedRef.current) return;
          if (p3.success) setPolicy(p3.data);
          if (savedClients !== null) {
            setHasOwnAppPolicy(savedClients.length > 0);
            const disp =
              savedClients.length > 0
                ? clientsToSelected(savedClients)
                : p3.success &&
                    p3.data.allowed_clients_source !== 'user' &&
                    p3.data.allowed_clients
                  ? clientsToSelected(p3.data.allowed_clients)
                  : [...ALL_CLIENTS];
            setLoadedSelected(disp);
            setSelected(disp);
          }
          setFailedSection('models');
          toast({ type: 'error', message: mr.error, auto_dismiss_ms: 4000 });
          return;
        }
        savedAliases = mr.data.modelAliases;
      }

      // 3) rate limit — 자식 패널이 실패 토스트를 직접 띄운다.
      if (rlDirty && !(await rateLimitRef.current?.save())) {
        if (!mountedRef.current) return;
        setFailedSection('ratelimit');
        return;
      }

      // 모두 성공 — 유효 정책을 먼저 다시 읽어 출처 배지·상속 프리필을 갱신한다.
      const p2 = await getEffectivePolicyAction(node.id);
      if (!mountedRef.current) return;
      if (p2.success) setPolicy(p2.data);
      if (savedClients !== null) {
        setHasOwnAppPolicy(savedClients.length > 0);
        // [] 저장(override 해제) 후엔 상속 목록을 다시 표시 — 전체 체크 잔존은
        // 무제한처럼 읽히지만 실제 적용은 상위 정책이다.
        const disp =
          savedClients.length > 0
            ? clientsToSelected(savedClients)
            : p2.success &&
                p2.data.allowed_clients_source !== 'user' &&
                p2.data.allowed_clients
              ? clientsToSelected(p2.data.allowed_clients)
              : [...ALL_CLIENTS];
        setLoadedSelected(disp);
        setSelected(disp);
      }
      if (savedAliases !== null) {
        setLoadedModelAliases(savedAliases);
        // override 해제([]) 후엔 상속 목록을 다시 표시해야 한다 — 전부 미체크는
        // "차단" 처럼 읽히므로 유효 목록으로 채운다. 저장 전 policy 는 stale 이므로
        // 방금 다시 읽은 p2 만 쓰고, 실패하면 전체 허용으로 폴백한다.
        const effective = p2.success ? p2.data.allowed_models : null;
        const display =
          savedAliases.length > 0
            ? savedAliases
            : effective && effective.length > 0
              ? effective
              : models.map((mm) => mm.alias);
        setSelectedModelAliases(display);
        setModelBaseline(display);
      }
      toast({
        type: 'success',
        message: t('saveSuccess'),
        auto_dismiss_ms: 5000,
      });
      // 개별설정 점/팀 카운트는 트리 메타에서 오므로 저장 후 재조회해 stale 를 막는다.
      onSaved?.();
    });
  };

  const busy = isLoadPending || isSavePending;

  // 접근 권한·허용 모델 중 하나라도 loaded 상태에서 변경되면 적용 활성화.
  // 접근 권한은 canonical key 로 비교 ([]·전체선택 동일 취급, 순서 무관).
  // clientsLoaded=false 면 stale 상태가 dirty 로 보이지 않게 막는다.
  const accessDirty = clientsLoaded && clientsKey(selected) !== clientsKey(loadedSelected);
  // 모델 선택은 기준선(상속 프리필 포함)과 순서 무관 비교 — 상속값과 같은 선택은
  // dirty 가 아니며, 저장하면 상속이 개인 override 로 굳는다.
  const modelsDirty =
    modelsLoaded &&
    selectedModelAliases.slice().sort().join(',') !== modelBaseline.slice().sort().join(',');

  // 헤더 배지는 저장된 "유효" 상태를 보여야 한다 — 개인 override 가 없어
  // 상속이면 loaded* 은 비어 있지만 실제 제한은 팀/조직 정책이 정한다.
  // policy 로드 실패 + 자체 행 없음이면 유효 상태를 모르므로 "상속" 배지를
  // 보여 확정 오표기(제한인데 제한없음 표시)를 막는다.
  // 유효 목록이 카탈로그 전체를 덮으면(명시적 전체 허용 override) "제한 없음".
  const effClients = policy
    ? (policy.allowed_clients ?? [])
    : hasOwnAppPolicy
      ? selectedToClients(loadedSelected)
      : null;
  const appBadgeInherit = effClients === null;
  const appBadgeRestricted =
    effClients !== null && effClients.length > 0 && effClients.length < ALL_CLIENTS.length;
  const appBadgeCount = effClients?.length ?? 0;

  const effModels = policy
    ? (policy.allowed_models ?? [])
    : loadedModelAliases.length > 0
      ? loadedModelAliases // 개인 override 존재 = 명시적 제한
      : null;
  const modelBadgeInherit = effModels === null;
  const modelBadgeRestricted =
    effModels !== null &&
    effModels.length > 0 &&
    (models.length === 0 || effModels.length < models.length);
  const modelBadgeCount = effModels?.length ?? 0;
  const dirty = accessDirty || modelsDirty || rlDirty;
  const dirtyCount = (accessDirty ? 1 : 0) + (modelsDirty ? 1 : 0) + (rlDirty ? 1 : 0);

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // dirty 가 모두 해소되면(되돌리기/저장 성공) 실패 마커도 지운다 — 그대로 두면
  // 노드 전환까지 빨간 "저장 실패" 배지가 남는다.
  useEffect(() => {
    if (!dirty) setFailedSection(null);
  }, [dirty]);

  const btn = (active: boolean) =>
    [
      'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
      active
        ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
        : 'text-muted-foreground interactive',
    ].join(' ');

  return (
    <div>
      <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
      <div className="flex items-center gap-2 text-sm mb-2">
        <span className="text-muted-foreground">{t('email')}</span>
        <span className="font-medium">{email}</span>
      </div>
      <div className="flex items-center gap-2 text-sm mb-2">
        <span className="text-muted-foreground">{t('role')}</span>
        {role ? (
          <Badge tone={ROLE_TONE[role] ?? 'neutral'}>{t(`roleLabel.${role}` as 'roleLabel.ADMIN' | 'roleLabel.TEAM_LEADER' | 'roleLabel.DEVELOPER')}</Badge>
        ) : (
          <span className="font-medium">-</span>
        )}
      </div>
      <div className="flex items-center gap-2 text-sm mb-4">
        <span className="text-muted-foreground">{t('teamNameLabel')}</span>
        <span className="font-medium">{teamName}</span>
      </div>

      {/* sticky 요약 스트립 — 노드 타입 · 수정된 섹션 수 · 마지막 실패 섹션.
          긴 패널을 스크롤해도 "무엇이 바뀌었는지"가 헤더에서 읽힌다. */}
      {(dirty || failedSection) && (
        <div className="sticky top-2 z-20 mb-3 flex w-fit flex-wrap items-center gap-2 rounded-full border border-border bg-card/95 px-3 py-1.5 text-xs shadow-sm backdrop-blur">
          <span className="badge badge-neutral whitespace-nowrap">{tp('nodeTypeUser')}</span>
          {dirty && (
            <span className="text-muted-foreground whitespace-nowrap">
              {tp('modified')} {dirtyCount}
            </span>
          )}
          {failedSection && (
            <span className="text-destructive whitespace-nowrap">
              {tp('failed')}: {failedSection === 'apps' ? t('appAccess.title') : failedSection === 'models' ? t('userModels.title') : t('rateLimit.title')}
            </span>
          )}
        </div>
      )}

      <PolicySection
        title={t('appAccess.title')}
        autoOpen={accessDirty || failedSection === 'apps'}
        badges={
          <>
            {(clientsLoaded || policy) &&
              (appBadgeInherit ? (
                <span className="badge badge-neutral whitespace-nowrap">{tp('inherit')}</span>
              ) : appBadgeRestricted ? (
                <span className="badge badge-amber whitespace-nowrap">
                  {tp('restricted', { count: appBadgeCount })}
                </span>
              ) : (
                <span className="badge badge-teal whitespace-nowrap">{tp('unrestricted')}</span>
              ))}
            {policy && (
              <span className="badge badge-neutral whitespace-nowrap">
                {tp(SOURCE_KEY[policy.allowed_clients_source])}
              </span>
            )}
            {accessDirty && <span className="badge badge-sky whitespace-nowrap">{tp('modified')}</span>}
            {failedSection === 'apps' && (
              <span className="badge badge-pink whitespace-nowrap">{tp('failed')}</span>
            )}
          </>
        }
      >
        <p className="text-xs text-muted-foreground mb-2">
          {t('appAccess.description')}
        </p>
        {/* 상속 출처 캡션 — 개인 정책이 없을 때 토글 상태는 팀/조직 정책의 프리필이다.
            변경하면 개인 정책으로 저장됨을 명시(userModels 의 상속 캡션과 같은 규칙). */}
        {clientsLoaded &&
          (policy?.allowed_clients_source === 'team' ||
            policy?.allowed_clients_source === 'organization') && (
            <p className="text-xs text-muted-foreground mb-2">
              {policy.allowed_clients_source === 'team'
                ? t('appAccess.inheritTeam')
                : t('appAccess.inheritOrg')}
            </p>
          )}
        {/* 개별 설정 되돌리기 — 선택을 비우는 staged 변경이다. Apply 로 [] 가
            저장되면 override 행이 지워져 팀/조직 정책으로 복귀한다. policy 가
            null(로드 실패)이면 출처를 모르므로 버튼을 숨긴다(fail-closed). */}
        {clientsLoaded && policy?.allowed_clients_source === 'user' && (
          <div className="flex items-center justify-between gap-2 mb-2">
            <p className="text-xs text-muted-foreground">
              {t('appAccess.customCaption')}
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() => setSelected([])}
              className="pressable flex-shrink-0 rounded-apple-sm border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary disabled:opacity-50"
            >
              {t('appAccess.followTeam')}
            </button>
          </div>
        )}
        {isLoadPending ? (
          <div className="text-xs text-muted-foreground py-1 mb-3">{t('appAccess.loading')}</div>
        ) : (
          <div
            role="group"
            aria-label={t('appAccess.groupLabel')}
            className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1 mb-3"
          >
            {CLIENT_OPTIONS.map((o) => (
              <button
                key={o.value}
                type="button"
                onClick={() => toggleClient(o.value)}
                aria-pressed={selected.includes(o.value)}
                disabled={busy || !clientsLoaded}
                className={btn(selected.includes(o.value))}
              >
                {o.label}
              </button>
            ))}
          </div>
        )}
        {/* 빈 선택의 저장 결과 — 0개 체크는 "전면 거부"가 아니라 개인 정책 해제(상속)다. */}
        {clientsLoaded && selected.length === 0 && (
          <p className="text-xs text-amber-600 mb-3">{t('appAccess.emptyHint')}</p>
        )}
      </PolicySection>

      <PolicySection
        title={t('userModels.title')}
        autoOpen={modelsDirty || failedSection === 'models'}
        badges={
          <>
            {(modelsLoaded || loadedModelAliases.length > 0 || policy) &&
              (modelBadgeInherit ? (
                <span className="badge badge-neutral whitespace-nowrap">{tp('inherit')}</span>
              ) : modelBadgeRestricted ? (
                <span className="badge badge-amber whitespace-nowrap">
                  {tp('restricted', { count: modelBadgeCount })}
                </span>
              ) : (
                <span className="badge badge-teal whitespace-nowrap">{tp('unrestricted')}</span>
              ))}
            {policy && (
              <span className="badge badge-neutral whitespace-nowrap">
                {tp(SOURCE_KEY[policy.allowed_models_source])}
              </span>
            )}
            {modelsDirty && <span className="badge badge-sky whitespace-nowrap">{tp('modified')}</span>}
            {failedSection === 'models' && (
              <span className="badge badge-pink whitespace-nowrap">{tp('failed')}</span>
            )}
          </>
        }
      >
        <p className="text-xs text-muted-foreground mb-3">
          {t('userModels.hint')}
        </p>
        {/* 정책 출처 캡션 — 개인 override 가 없으면 체크박스의 체크 상태는
            팀 정책(또는 전체 허용)의 프리필이다. 변경하면 개인 정책으로 저장됨을
            명시한다. */}
        {policy && policy.allowed_models_source !== 'user' && (
          <p className="text-xs text-muted-foreground mb-3">
            {policy.allowed_models_source === 'team'
              ? t('userModelsInheritTeam')
              : t('userModelsInheritNone')}
          </p>
        )}
        {/* 개별 override 되돌리기 — staged 변경. Apply 시 [] 저장 = 행 삭제 =
            팀 정책 복귀. policy 로드 실패 시 출처를 모르므로 숨긴다. */}
        {modelsLoaded && policy?.allowed_models_source === 'user' && (
          <div className="flex items-center justify-between gap-2 mb-3">
            <p className="text-xs text-muted-foreground">
              {t('userModelsCustomCaption')}
            </p>
            <button
              type="button"
              disabled={busy}
              onClick={() => setSelectedModelAliases([])}
              className="pressable flex-shrink-0 rounded-apple-sm border border-border px-2.5 py-1 text-xs font-medium text-muted-foreground transition-colors hover:border-primary/50 hover:text-primary disabled:opacity-50"
            >
              {t('userModels.followTeam')}
            </button>
          </div>
        )}
        {!modelsLoaded ? (
          <div className="text-xs text-destructive py-1">
            {t('userModels.loadFailed')}
          </div>
        ) : models.length === 0 ? (
          <div className="text-xs text-muted-foreground py-1">{t('userModels.empty')}</div>
        ) : (
          <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
            {models.map((m) => (
              <label
                key={m.alias}
                className="flex items-center gap-2 rounded-apple-sm border p-2 cursor-pointer hover:bg-muted/50"
              >
                <input
                  type="checkbox"
                  checked={selectedModelAliases.includes(m.alias)}
                  onChange={() => toggleModel(m.alias)}
                  disabled={busy || !modelsLoaded}
                  className="h-4 w-4 rounded border-gray-300"
                />
                <span className="text-sm">{m.display_name || m.alias}</span>
              </label>
            ))}
          </div>
        )}
      </PolicySection>

      <PolicySection
        title={t('rateLimit.title')}
        autoOpen={rlDirty || failedSection === 'ratelimit'}
        badges={
          <>
            {rlSummary.loaded &&
              (rlSummary.restricted ? (
                <span className="badge badge-amber whitespace-nowrap">
                  {tp('limited', { count: rlSummary.count })}
                </span>
              ) : (
                <span className="badge badge-teal whitespace-nowrap">{tp('unrestricted')}</span>
              ))}
            {rlSummary.loaded && rlSummary.source === 'team' && (
              <span className="badge badge-neutral whitespace-nowrap">{tp('sourceTeam')}</span>
            )}
            {rlSummary.loaded && rlSummary.source === 'own' && (
              <span className="badge badge-neutral whitespace-nowrap">{tp('sourceOwn')}</span>
            )}
            {rlDirty && <span className="badge badge-sky whitespace-nowrap">{tp('modified')}</span>}
            {failedSection === 'ratelimit' && (
              <span className="badge badge-pink whitespace-nowrap">{tp('failed')}</span>
            )}
          </>
        }
      >
        <ScopeRateLimitPanel
          ref={rateLimitRef}
          scope="user"
          scopeId={node.id}
          inheritedFromLabel={node.meta.team_name ?? undefined}
          hideActions
          bare
          disabled={busy}
          onDirtyChange={setRlDirty}
          onSummaryChange={setRlSummary}
        />
      </PolicySection>

      <PolicySection
        title={t('budgetInput.title')}
        badges={
          <span className="badge badge-neutral whitespace-nowrap">{tp('readonly')}</span>
        }
      >
        <div className="flex items-center justify-end mb-2">
          <Link
            href="/budgets"
            className="text-xs text-primary hover:underline"
          >
            {t('budgetInput.editInBudgets')}
          </Link>
        </div>
        {/* 총예산(사용자/팀) + 앱별 예산을 게이지로 표시. 앱별은 접근 허용과 무관하게
            전체 앱을 보여준다 — 허용되지 않은 앱에 설정된 예산(고아 예산)도
            여기서 보여야 발견할 수 있다. */}
        <div className="space-y-2">
          {policy?.budgets
            .filter((b) => (b.scope === 'USER' && b.client === null) || b.scope === 'TEAM')
            .map((b, i) => (
              <BudgetGaugeRow
                key={`total-${i}`}
                label={b.scope === 'TEAM' ? t('budgetInput.teamTotal') : t('budgetInput.userTotal')}
                max={b.max_budget_usd}
                used={b.used_usd}
                unsetLabel={t('budgetInput.placeholder')}
              />
            ))}
          {CLIENT_OPTIONS.map((o) => {
            const cfg = policy?.budgets.find(
              (b) => b.scope === 'USER' && b.client === o.value,
            );
            return (
              <BudgetGaugeRow
                key={o.value}
                label={o.label}
                max={cfg?.max_budget_usd ?? null}
                used={cfg?.used_usd ?? null}
                unsetLabel={t('budgetInput.placeholder')}
              />
            );
          })}
        </div>
      </PolicySection>

      <PolicySection
        title={t('effectivePolicy.title')}
        badges={
          <span className="badge badge-neutral whitespace-nowrap">{tp('readonly')}</span>
        }
      >
        <p className="text-xs text-muted-foreground mb-3">
          {t('effectivePolicy.hint')}
        </p>
        <EffectivePolicyCard userId={node.id} policy={policy} models={models} />
      </PolicySection>

      {/* 플로팅 Apply 바 — dirty 일 때만 뜬다. 공용 UnsavedApplyBar 와 통일. */}
      {dirty && (
        <UnsavedApplyBar
          isPending={isSavePending}
          disabled={busy}
          onApply={handleApply}
          items={[
            ...(accessDirty
              ? [
                  {
                    key: 'apps',
                    label: t('appAccess.title'),
                    onRevert: () => setSelected(loadedSelected),
                  },
                ]
              : []),
            ...(modelsDirty
              ? [
                  {
                    key: 'models',
                    label: t('userModels.title'),
                    onRevert: () => setSelectedModelAliases(modelBaseline),
                  },
                ]
              : []),
            ...(rlDirty
              ? [
                  {
                    key: 'ratelimit',
                    label: t('rateLimit.title'),
                    onRevert: () => rateLimitRef.current?.revert(),
                  },
                ]
              : []),
          ]}
        />
      )}
    </div>
  );
}

// ── TEAM 상세 (강제 재인증 버튼 포함) ─────────────────────────────────────────

function TeamPanel({
  node,
  onDirtyChange,
  orgId,
}: {
  node: OrgTreeNode;
  onDirtyChange?: (_dirty: boolean) => void;
  orgId?: string;
}) {
  const t = useTranslations('users');
  const tm = useTranslations('models');
  const tc = useTranslations('common');
  const tp = useTranslations('policyState');
  const { toast } = useToast();
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [isLeaderPending, startLeaderTransition] = useTransition();
  const [selectedMemberId, setSelectedMemberId] = useState('');
  const [teamModels, setTeamModels] = useState<ModelListItem[]>([]);
  // 모델 카탈로그 로드 상태 — 실패를 추적해야 "빈 목록 = 무제한" 오표기와
  // stale [] 저장(기존 제한 삭제 사고)을 막을 수 있다.
  const [catalogStatus, setCatalogStatus] = useState<'loading' | 'ready' | 'failed'>('loading');
  // 해제할 리더를 확인 모달에서 명확히 지정 — 팀에 리더가 여러 명일 수 있으므로
  // "리더 해제" 버튼 하나로는 어느 사람을 내릴지 알 수 없다.
  const [leaderToRemove, setLeaderToRemove] = useState<{ id: string; name: string } | null>(null);

  // 앱 접근 + 모델 권한 통합 Apply — 두 패널은 자체 버튼을 숨기고(hideActions)
  // dirty/save 를 이 ref·콜백에 맡긴다. UserPanel 의 플로팅 바와 같은 패턴.
  const appAccessRef = useRef<ScopeAppAccessHandle>(null);
  const modelPolicyRef = useRef<TeamModelPermissionHandle>(null);
  const rateLimitRef = useRef<ScopeRateLimitHandle>(null);
  const [appDirty, setAppDirty] = useState(false);
  const [modelDirty, setModelDirty] = useState(false);
  const [rlDirty, setRlDirty] = useState(false);
  const [isPolicySavePending, startPolicySaveTransition] = useTransition();
  const policyDirty = appDirty || modelDirty || rlDirty;
  const dirtyCount = (appDirty ? 1 : 0) + (modelDirty ? 1 : 0) + (rlDirty ? 1 : 0);
  // 섹션 헤더 배지용 저장 상태 요약 — 자식 패널이 로드할 때마다 보고한다.
  const [appSummary, setAppSummary] = useState<PolicySummary>(EMPTY_POLICY_SUMMARY);
  const [modelSummary, setModelSummary] = useState<PolicySummary>(EMPTY_POLICY_SUMMARY);
  const [rlSummary, setRlSummary] = useState<PolicySummary>(EMPTY_POLICY_SUMMARY);
  // 통합 Apply 의 실패 섹션 — 헤더 "저장 실패" 배지 + 자동 펼침에 쓴다.
  const [failedSection, setFailedSection] = useState<'apps' | 'models' | 'ratelimit' | null>(null);

  useEffect(() => {
    onDirtyChange?.(policyDirty);
  }, [policyDirty, onDirtyChange]);

  // dirty 가 모두 해소되면(되돌리기/저장 성공) 실패 마커도 지운다.
  useEffect(() => {
    if (!policyDirty) setFailedSection(null);
  }, [policyDirty]);

  const handleApplyAll = () => {
    startPolicySaveTransition(async () => {
      setFailedSection(null);
      // 앱 접근 → 모델 순서로 저장(UserPanel 과 동일). 앞이 실패하면 뒤는 저장하지 않는다.
      if (appDirty && !(await appAccessRef.current?.save())) {
        setFailedSection('apps');
        return;
      }
      if (modelDirty && !(await modelPolicyRef.current?.save())) {
        setFailedSection('models');
        return;
      }
      if (rlDirty && !(await rateLimitRef.current?.save())) {
        setFailedSection('ratelimit');
        return;
      }
      toast({ type: 'success', message: t('saveSuccess'), auto_dismiss_ms: 5000 });
    });
  };

  // 팀별 허용 모델 패널용 모델 목록 — 팀 상세가 열릴 때만 로드한다.
  // 실패는 ready 로 덮지 않는다: 카탈로그 미로드 상태의 섹션은 저장 불가여야 한다.
  useEffect(() => {
    setCatalogStatus('loading');
    listActiveModelsAction().then((r) => {
      if (r.success) {
        setTeamModels(r.data);
        setCatalogStatus('ready');
      } else {
        setCatalogStatus('failed');
      }
    });
  }, []);

  const memberCount = node.meta.member_count ?? 0;
  const members = node.children ?? [];
  // 리더 목록은 트리에 이미 실려오는 멤버별 role 로 계산 — 팀 하나에 여러 명일 수 있다
  // (role=TEAM_LEADER 는 팀당 배타적 단일값이 아니라 팀원 각자의 속성).
  const leaders = members.filter((m) => m.meta.role === 'TEAM_LEADER');
  const nonLeaderMembers = members.filter((m) => m.meta.role !== 'TEAM_LEADER');

  const handleForceReauth = () => {
    startTransition(async () => {
      const result = await forceReauthTeamAction(node.id);
      setConfirmOpen(false);
      if (result.success) {
        toast({
          type: 'success',
          message: t('forceReauth.success', { count: result.data.revoked_count }),
          auto_dismiss_ms: 5000,
        });
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
      }
    });
  };

  const handleAssignLeader = () => {
    if (!selectedMemberId) return;
    startLeaderTransition(async () => {
      const result = await setTeamLeaderAction(selectedMemberId, node.id);
      if (result.success) {
        toast({ type: 'success', message: t('leaderAction.assignSuccess'), auto_dismiss_ms: 4000 });
        setSelectedMemberId('');
        router.refresh();
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
      }
    });
  };

  const handleConfirmRemoveLeader = () => {
    if (!leaderToRemove) return;
    startLeaderTransition(async () => {
      const result = await unsetTeamLeaderAction(node.id, leaderToRemove.id);
      setLeaderToRemove(null);
      if (result.success) {
        toast({ type: 'success', message: t('leaderAction.unassignSuccess'), auto_dismiss_ms: 4000 });
        router.refresh();
      } else {
        toast({ type: 'error', message: result.error, auto_dismiss_ms: 4000 });
      }
    });
  };

  return (
    <div>
      <h2 className="text-lg font-semibold mb-4">{node.name}</h2>
      <div className="flex items-center gap-2 text-sm mb-4">
        <span className="text-muted-foreground">{t('memberCount')}</span>
        <span className="font-medium">{t('memberCountValue', { count: memberCount })}</span>
      </div>

      {/* sticky 요약 스트립 — 노드 타입 · 수정된 섹션 수 · 마지막 실패 섹션 */}
      {(policyDirty || failedSection) && (
        <div className="sticky top-2 z-20 mb-3 flex w-fit flex-wrap items-center gap-2 rounded-full border border-border bg-card/95 px-3 py-1.5 text-xs shadow-sm backdrop-blur">
          <span className="badge badge-neutral whitespace-nowrap">{tp('nodeTypeTeam')}</span>
          {policyDirty && (
            <span className="text-muted-foreground whitespace-nowrap">
              {tp('modified')} {dirtyCount}
            </span>
          )}
          {failedSection && (
            <span className="text-destructive whitespace-nowrap">
              {tp('failed')}: {failedSection === 'apps' ? t('scopeAppAccess.title') : failedSection === 'models' ? tm('teamModelAccess') : t('rateLimit.title')}
            </span>
          )}
        </div>
      )}

      <div className="border rounded-apple-md p-3 mb-4">
        <p className="text-sm font-medium mb-2">{t('leaderAction.currentLeaders')}</p>
        {leaders.length === 0 ? (
          <p className="text-xs text-muted-foreground mb-3">{t('leaderAction.noLeaders')}</p>
        ) : (
          <ul className="flex flex-col gap-1.5 mb-3">
            {leaders.map((leaderNode) => (
              <li
                key={leaderNode.id}
                className="flex items-center justify-between gap-2 rounded-apple-sm border px-3 py-1.5"
              >
                <span className="text-sm">{leaderNode.meta.email}</span>
                <button
                  type="button"
                  disabled={isLeaderPending}
                  onClick={() => setLeaderToRemove({ id: leaderNode.id, name: leaderNode.name })}
                  className="text-xs text-destructive hover:underline disabled:opacity-50"
                >
                  {t('leaderAction.unassignButton')}
                </button>
              </li>
            ))}
          </ul>
        )}

        {nonLeaderMembers.length === 0 ? (
          leaders.length === 0 && <p className="text-xs text-muted-foreground">{t('leaderAction.noMembers')}</p>
        ) : (
          <div className="flex items-center gap-2">
            <select
              value={selectedMemberId}
              onChange={(e) => setSelectedMemberId(e.target.value)}
              disabled={isLeaderPending}
              className="glass flex-1 rounded-apple-sm border px-3 py-1.5 text-sm disabled:opacity-50"
            >
              <option value="">{t('leaderAction.selectPlaceholder')}</option>
              {nonLeaderMembers.map((m) => (
                <option key={m.id} value={m.id} disabled={m.meta.role === 'ADMIN'}>
                  {m.meta.email}
                  {m.meta.role === 'ADMIN' ? ' [Admin]' : ''}
                </option>
              ))}
            </select>
            <SpinnerButton
              type="button"
              isLoading={isLeaderPending}
              disabled={!selectedMemberId}
              onClick={handleAssignLeader}
            >
              {t('leaderAction.assignButton')}
            </SpinnerButton>
          </div>
        )}
      </div>

      <AppDialog
        isOpen={leaderToRemove !== null}
        onClose={() => setLeaderToRemove(null)}
        title={t('leaderAction.removeModalTitle')}
      >
        {leaderToRemove && (
          <>
            <p className="text-sm text-muted-foreground mb-2">
              {t('leaderAction.removeModalBody', { name: leaderToRemove.name })}
            </p>
            <p className="text-sm text-muted-foreground mb-4">{t('leaderAction.removeModalNote')}</p>
            <div className="flex justify-end gap-2">
              <button
                type="button"
                onClick={() => setLeaderToRemove(null)}
                disabled={isLeaderPending}
                className="px-3 py-1.5 text-sm rounded-md border hover:bg-muted"
              >
                {tc('cancel')}
              </button>
              <SpinnerButton
                type="button"
                className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
                isLoading={isLeaderPending}
                onClick={handleConfirmRemoveLeader}
              >
                {t('leaderAction.unassignButton')}
              </SpinnerButton>
            </div>
          </>
        )}
      </AppDialog>

      <PolicySection
        title={t('scopeAppAccess.title')}
        autoOpen={appDirty || failedSection === 'apps'}
        badges={
          <>
            {appSummary.loaded &&
              (appSummary.restricted ? (
                <span className="badge badge-amber whitespace-nowrap">
                  {tp('restricted', { count: appSummary.count })}
                </span>
              ) : (
                <span className="badge badge-teal whitespace-nowrap">{tp('unrestricted')}</span>
              ))}
            {appDirty && <span className="badge badge-sky whitespace-nowrap">{tp('modified')}</span>}
            {failedSection === 'apps' && (
              <span className="badge badge-pink whitespace-nowrap">{tp('failed')}</span>
            )}
          </>
        }
      >
        <p className="text-xs text-muted-foreground mb-3">
          {t('scopeAppAccess.descriptionTeam')}
        </p>
        <ScopeAppAccessPanel
          ref={appAccessRef}
          scope="team"
          scopeId={node.id}
          orgScopeId={orgId}
          hideActions
          bare
          disabled={isPolicySavePending}
          onDirtyChange={setAppDirty}
          onSummaryChange={setAppSummary}
        />
      </PolicySection>

      <PolicySection
        title={tm('teamModelAccess')}
        autoOpen={modelDirty || failedSection === 'models'}
        badges={
          <>
            {modelSummary.loaded &&
              (modelSummary.restricted ? (
                <span className="badge badge-amber whitespace-nowrap">
                  {tp('restricted', { count: modelSummary.count })}
                </span>
              ) : (
                <span className="badge badge-teal whitespace-nowrap">{tp('unrestricted')}</span>
              ))}
            {modelDirty && <span className="badge badge-sky whitespace-nowrap">{tp('modified')}</span>}
            {failedSection === 'models' && (
              <span className="badge badge-pink whitespace-nowrap">{tp('failed')}</span>
            )}
          </>
        }
      >
        {/* 모델 카탈로그가 로드되기 전/실패 상태에서는 패널을 마운트하지 않는다 —
            빈 카탈로그로 렌더하면 "전체 미체크 = 무제한" 으로 읽히고, 저장 시
            stale [] 가 기존 팀 제한을 지운다. */}
        {catalogStatus === 'failed' ? (
          <div className="text-xs text-destructive py-1">{tm('loadFailed')}</div>
        ) : catalogStatus === 'loading' ? (
          <div className="text-xs text-muted-foreground py-1">{tm('loadingText')}</div>
        ) : (
          <TeamModelPermissionPanel
            ref={modelPolicyRef}
            teamId={node.id}
            models={teamModels}
            hideActions
            bare
            disabled={isPolicySavePending}
            onDirtyChange={setModelDirty}
            onSummaryChange={setModelSummary}
          />
        )}
      </PolicySection>

      <PolicySection
        title={t('rateLimit.title')}
        autoOpen={rlDirty || failedSection === 'ratelimit'}
        badges={
          <>
            {rlSummary.loaded &&
              (rlSummary.restricted ? (
                <span className="badge badge-amber whitespace-nowrap">
                  {tp('limited', { count: rlSummary.count })}
                </span>
              ) : (
                <span className="badge badge-teal whitespace-nowrap">{tp('unrestricted')}</span>
              ))}
            {rlSummary.loaded && rlSummary.source === 'own' && (
              <span className="badge badge-neutral whitespace-nowrap">{tp('sourceOwn')}</span>
            )}
            {rlDirty && <span className="badge badge-sky whitespace-nowrap">{tp('modified')}</span>}
            {failedSection === 'ratelimit' && (
              <span className="badge badge-pink whitespace-nowrap">{tp('failed')}</span>
            )}
          </>
        }
      >
        <ScopeRateLimitPanel
          ref={rateLimitRef}
          scope="team"
          scopeId={node.id}
          hideActions
          bare
          disabled={isPolicySavePending}
          onDirtyChange={setRlDirty}
          onSummaryChange={setRlSummary}
        />
      </PolicySection>

      {/* 예산·다운그레이드는 예산 페이지 소유 — 여기선 진입 링크만 둔다. */}
      <div className="mb-4 flex items-center justify-between rounded-apple-md border px-3 py-2.5">
        <span className="text-sm font-medium">{t('budgetSectionTitle')}</span>
        <Link href="/budgets" className="text-xs text-primary hover:underline">
          {t('budgetInput.editInBudgets')}
        </Link>
      </div>

      {/* 통합 Apply — 앱 접근/모델 권한 어느 쪽이든 dirty 면 뜬다.
          공용 UnsavedApplyBar 와 통일. */}
      {policyDirty && (
        <UnsavedApplyBar
          isPending={isPolicySavePending}
          onApply={handleApplyAll}
          items={[
            ...(appDirty
              ? [
                  {
                    key: 'apps',
                    label: t('scopeAppAccess.title'),
                    onRevert: () => appAccessRef.current?.revert(),
                  },
                ]
              : []),
            ...(modelDirty
              ? [
                  {
                    key: 'models',
                    label: tm('teamModelAccess'),
                    onRevert: () => modelPolicyRef.current?.revert(),
                  },
                ]
              : []),
            ...(rlDirty
              ? [
                  {
                    key: 'ratelimit',
                    label: t('rateLimit.title'),
                    onRevert: () => rateLimitRef.current?.revert(),
                  },
                ]
              : []),
          ]}
        />
      )}

      <SpinnerButton
        type="button"
        className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
        onClick={() => setConfirmOpen(true)}
        disabled={memberCount === 0}
      >
        {t('forceReauth.button')}
      </SpinnerButton>

      <AppDialog
        isOpen={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        title={t('forceReauth.button')}
      >
        <p className="text-sm text-muted-foreground mb-2">
          <span className="font-medium text-foreground">{node.name}</span>{' '}
          {t('forceReauth.warning', { count: memberCount })}
        </p>
        <p className="text-sm text-muted-foreground mb-4">
          {t('forceReauth.note')}
        </p>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            onClick={() => setConfirmOpen(false)}
            disabled={isPending}
            className="px-3 py-1.5 text-sm rounded-md border hover:bg-muted"
          >
            {tc('cancel')}
          </button>
          <SpinnerButton
            type="button"
            className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
            isLoading={isPending}
            onClick={handleForceReauth}
          >
            {t('forceReauth.proceed')}
          </SpinnerButton>
        </div>
      </AppDialog>
    </div>
  );
}