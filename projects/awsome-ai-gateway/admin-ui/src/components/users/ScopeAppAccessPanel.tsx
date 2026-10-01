'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// 팀/조직 단위 앱 접근 정책 (alembic 0038).
// 폴백 체인 user > team > org > 제한없음 — 빈 목록([])은 "이 레벨 정책 없음"이며
// 상위 정책을 상속한다. 조직이 최상위라 조직의 [] 는 무제한이다.
//
// 저장 규칙(Option A — 체크한 목록을 그대로 저장):
//   · 빈 선택      → [] 저장 = 이 레벨 정책 해제(팀: 조직 상속, 조직: 무제한)
//   · 부분 선택    → 명시 화이트리스트
//   · 전체 선택    → 팀은 명시 목록(상속을 끊는 전체 허용 override),
//                    조직(리프)은 [] 로 정규화 — 무제한의 정식 표현
// 팀 패널은 조직 정책도 함께 조회한다 — 팀 행이 없으면 화면에 조직 상속값을
// 프리필해 보여주고, 헤더 배지(onSummaryChange)는 **유효** 상태를 보고한다.
//
// hideActions=true 로 임베드하면 자체 Apply 를 숨기고 ref.save() / onDirtyChange 를
// 부모의 통합 저장(OrgDetailPanel TeamPanel 의 플로팅 Apply 바)에 맡긴다.

import { forwardRef, useImperativeHandle, useState, useEffect, useRef, useTransition } from 'react';
import { useTranslations } from 'next-intl';
import {
  getScopeAllowedClientsAction,
  setScopeAllowedClientsAction,
} from '@/lib/actions/users';
import { CLIENTS, CLIENT_LABELS, type GatewayClient } from '@/lib/constants/gateway';
import { UnsavedApplyBar } from '@/components/common/UnsavedApplyBar';
import { useToast } from '@/components/common/ToastProvider';
import type { PolicySummary } from '@/components/common/policySummary';

const ALL_CLIENTS = CLIENTS;
type ClientId = GatewayClient;

// API rows → 체크 상태. [] = 이 레벨 정책 없음 = 전부 체크로 표시.
function clientsToSelected(clients: string[]): ClientId[] {
  if (clients.length === 0) return [...ALL_CLIENTS];
  return ALL_CLIENTS.filter((c) => clients.includes(c));
}

export interface ScopeAppAccessHandle {
  /** 통합 저장 경로 — 성공 시 true. 실패 시 토스트를 띄우고 false. */
  save: () => Promise<boolean>;
  /** 편집을 마지막 저장 상태로 되돌린다 (통합 Apply 바의 섹션별 되돌리기). */
  revert: () => void;
}

interface ScopeAppAccessPanelProps {
  scope: 'team' | 'organization';
  scopeId: string;
  /** team 스코프의 상속 표시용 — 트리 root(ORGANIZATION) 노드 id. 없으면 상속 표시 생략. */
  orgScopeId?: string;
  /** 자체 Apply 버튼 숨김 — 부모의 통합 저장이 ref.save() 를 호출한다. */
  hideActions?: boolean;
  /** 헤더(제목/설명/배지) 숨김 — PolicySection 헤더가 대신 보여준다. */
  bare?: boolean;
  /** 부모 저장 진행 중 — 편집을 잠근다(저장 중 토글이 다음 저장에 섞이는 것 방지). */
  disabled?: boolean;
  onDirtyChange?: (_dirty: boolean) => void;
  /** 저장된 정책 요약을 부모(섹션 헤더 배지)에 보고한다 — 유효(상속 반영) 상태. */
  onSummaryChange?: (_summary: PolicySummary) => void;
}

export const ScopeAppAccessPanel = forwardRef<ScopeAppAccessHandle, ScopeAppAccessPanelProps>(
  function ScopeAppAccessPanel({ scope, scopeId, orgScopeId, hideActions, bare, disabled, onDirtyChange, onSummaryChange }, ref) {
    const t = useTranslations('users.scopeAppAccess');
    const { toast } = useToast();
    const [isLoadPending, startLoadTransition] = useTransition();
    const [isSavePending, startSaveTransition] = useTransition();

    const [loadedSelected, setLoadedSelected] = useState<ClientId[]>([...ALL_CLIENTS]);
    const [selected, setSelected] = useState<ClientId[]>([...ALL_CLIENTS]);
    // 자체 정책 행(저장된 raw rows) — [] 이면 이 레벨 정책 없음.
    const [ownClients, setOwnClients] = useState<string[]>([]);
    // 팀 스코프 전용 상속 대상 — null=미조회/실패, []=조직 무제한.
    const [orgClients, setOrgClients] = useState<string[] | null>(null);
    // 정책이 정상 로드됐는지 추적 — 로드 실패 상태에서 저장하면 stale 전체허용이
    // 기존 제한을 덮어쓸 수 있으므로 저장·편집을 차단한다(UserPanel 과 같은 규칙).
    const [loaded, setLoaded] = useState(false);
    // 언마운트 후 setState 방지 — 저장 후 재조회는 로드 effect의 cancelled 가드 밖이다.
    const mountedRef = useRef(true);
    useEffect(() => () => {
      mountedRef.current = false;
    }, []);

    /** 체크 상태 → 저장용 rows. 빈 선택 = [](정책 해제=상속). 리프(조직)는 전체도 []. */
    const toSaved = (sel: ClientId[]): string[] => {
      const chosen = ALL_CLIENTS.filter((c) => sel.includes(c));
      if (chosen.length === 0) return [];
      if (scope === 'organization' && chosen.length === ALL_CLIENTS.length) return [];
      return chosen;
    };

    useEffect(() => {
      let cancelled = false;
      setLoaded(false);
      setOwnClients([]);
      setOrgClients(null);
      startLoadTransition(async () => {
        const r = await getScopeAllowedClientsAction(scope, scopeId);
        // 빠른 scope 전환에서 늦게 돌아온 응답이 새 상태를 덮지 않게 한다.
        if (cancelled) return;
        if (!r.success) {
          toast({ type: 'error', message: t('loadError'), auto_dismiss_ms: 5000 });
          return;
        }
        const own = r.data.clients;
        // 팀 정책이 없으면 조직 정책을 읽어 상속 프리필 + 유효 배지에 쓴다.
        let org: string[] | null = null;
        if (scope === 'team' && own.length === 0 && orgScopeId) {
          const g = await getScopeAllowedClientsAction('organization', orgScopeId);
          if (cancelled) return;
          if (g.success) {
            org = g.data.clients;
          } else {
            toast({ type: 'error', message: t('loadError'), auto_dismiss_ms: 5000 });
          }
        }
        setOwnClients(own);
        setOrgClients(org);
        // 자체 행 없음 → 상속된 유효 목록(조직 또는 전체 허용)을 프리필해 보여준다 —
        // 빈 전체체크는 "제한 없음"으로 오독되므로 실제 적용값을 표시한다.
        const display =
          own.length > 0
            ? clientsToSelected(own)
            : org !== null && org.length > 0
              ? clientsToSelected(org)
              : [...ALL_CLIENTS];
        setLoadedSelected(display);
        setSelected(display);
        setLoaded(true);
      });
      return () => {
        cancelled = true;
      };
    }, [scope, scopeId, orgScopeId, t, toast]);

    const toggle = (c: ClientId) => {
      setSelected((prev) =>
        prev.includes(c) ? prev.filter((x) => x !== c) : [...prev, c],
      );
    };

    const busy = isLoadPending || isSavePending || disabled === true;
    const dirty =
      loaded &&
      toSaved(selected).join(',') !== toSaved(loadedSelected).join(',');

    useEffect(() => {
      onDirtyChange?.(dirty);
    }, [dirty, onDirtyChange]);

    // 헤더 배지는 저장된 **유효** 상태만 보여준다 — 팀 자체 행이 없으면
    // 조직 정책이 실제 적용값이다. 편집 중 값은 dirty 마커가 담당.
    useEffect(() => {
      const effective =
        ownClients.length > 0
          ? ownClients
          : scope === 'organization'
            ? []
            : (orgClients ?? null);
      const summaryReady =
        loaded && (effective !== null || ownClients.length > 0);
      onSummaryChange?.({
        loaded: summaryReady,
        restricted: effective !== null && effective.length > 0 && effective.length < ALL_CLIENTS.length,
        count: effective?.length ?? 0,
      });
    }, [loaded, ownClients, orgClients, scope, onSummaryChange]);

    const save = async (): Promise<boolean> => {
      // 로드 실패 상태의 저장은 무음 false 가 아니라 명시적 차단이어야 한다 —
      // stale 전체허용이 기존 제한을 덮는 사고 방지.
      if (!loaded) {
        toast({ type: 'error', message: t('loadError'), auto_dismiss_ms: 4000 });
        return false;
      }
      const r = await setScopeAllowedClientsAction(scope, scopeId, toSaved(selected));
      if (!mountedRef.current) return r.success;
      if (!r.success) {
        toast({ type: 'error', message: r.error, auto_dismiss_ms: 4000 });
        return false;
      }
      const saved = r.data.clients;
      // [] 저장(정책 해제) 후엔 상속값으로 다시 채운다 — 전체 체크 잔존은
      // 무제한처럼 읽히지만 실제 적용은 조직 정책이다. 조직도 다시 읽는다.
      let org = orgClients;
      if (scope === 'team' && saved.length === 0 && orgScopeId) {
        const g = await getScopeAllowedClientsAction('organization', orgScopeId);
        if (!mountedRef.current) return true;
        if (g.success) org = g.data.clients;
        setOrgClients(org);
      }
      if (!mountedRef.current) return true;
      const display =
        saved.length > 0
          ? clientsToSelected(saved)
          : org !== null && org.length > 0
            ? clientsToSelected(org)
            : [...ALL_CLIENTS];
      setOwnClients(saved);
      setLoadedSelected(display);
      setSelected(display);
      if (!hideActions) {
        toast({ type: 'success', message: t('saveSuccess'), auto_dismiss_ms: 4000 });
      }
      return true;
    };

    const revert = () => {
      setSelected(loadedSelected);
    };

    useImperativeHandle(ref, () => ({ save, revert }));

    const handleApply = () => {
      startSaveTransition(async () => {
        await save();
      });
    };

    const btn = (active: boolean) =>
      [
        'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
        active
          ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
          : 'text-muted-foreground interactive',
      ].join(' ');

    // 저장된 유효 상태 배지 (비임베드 헤더용)
    const effectiveForBadge =
      ownClients.length > 0
        ? ownClients
        : scope === 'organization'
          ? []
          : (orgClients ?? []);

    return (
      <div className={bare ? '' : 'border rounded-apple-md p-3'}>
        {!bare && (
          <>
            <div className="flex items-center gap-2 mb-1">
              <p className="text-sm font-medium">{t('title')}</p>
              {loaded &&
                (effectiveForBadge.length === 0 || effectiveForBadge.length === ALL_CLIENTS.length ? (
                  <span className="badge badge-teal">{t('unrestricted')}</span>
                ) : (
                  <span className="badge badge-amber">
                    {t('restricted', { count: effectiveForBadge.length })}
                  </span>
                ))}
            </div>
            <p className="text-xs text-muted-foreground mb-3">
              {scope === 'team' ? t('descriptionTeam') : t('descriptionOrg')}
            </p>
          </>
        )}
        {isLoadPending ? (
          <div className="text-xs text-muted-foreground py-1">{t('loading')}</div>
        ) : (
          <div className="flex items-center gap-3 flex-wrap">
            <div
              role="group"
              aria-label={t('groupLabel')}
              className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1"
            >
              {ALL_CLIENTS.map((c) => (
                <button
                  key={c}
                  type="button"
                  onClick={() => toggle(c)}
                  aria-pressed={selected.includes(c)}
                  disabled={busy || !loaded}
                  className={btn(selected.includes(c))}
                >
                  {CLIENT_LABELS[c]}
                </button>
              ))}
            </div>
          </div>
        )}
        {/* 빈 선택의 저장 결과를 미리 알린다 — 0개 체크는 "전면 거부"가 아니라
            이 레벨 정책 해제(상속/무제한)다. */}
        {loaded && selected.length === 0 && (
          <p className="text-xs text-amber-600 pt-1">
            {t(scope === 'team' ? 'emptyHintTeam' : 'emptyHintOrg')}
          </p>
        )}
        {/* 단독 사용(hideActions=false)일 때도 UserPanel 과 같은 플로팅 Apply —
            인라인 버튼은 스크롤 위치에 따라 안 보인다. */}
        {!hideActions && dirty && (
          <UnsavedApplyBar isPending={isSavePending} disabled={busy} onApply={handleApply} />
        )}
      </div>
    );
  },
);
