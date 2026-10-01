'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { forwardRef, useImperativeHandle, useState, useTransition, useEffect, useMemo } from 'react';
import { useTranslations } from 'next-intl';
import { useToast } from '@/components/common/ToastProvider';
import { SpinnerButton } from '@/components/common/SpinnerButton';
import {
  getTeamAllowedModelsAction,
  setTeamAllowedModelsAction,
} from '@/lib/actions/models';
import type { ModelListItem } from '@/types/entities';
import type { PolicySummary } from '@/components/common/policySummary';

interface TeamOption {
  id: string;
  name: string;
  // 팀명이 부서 간 중복될 수 있어(예: 여러 부서에 "Developers" 팀), 드롭다운
  // 표시에 부서명을 병기해 구분한다.
  department_name: string | null;
}

export interface TeamModelPermissionHandle {
  /** 통합 저장 경로 — 성공 시 true. 실패 시 토스트를 띄우고 false. */
  save: () => Promise<boolean>;
  /** 편집을 마지막 저장 상태로 되돌린다 (통합 Apply 바의 섹션별 되돌리기). */
  revert: () => void;
}

interface TeamModelPermissionPanelProps {
  // 팀 상세(OrgDetailPanel)에 임베드될 때는 teamId 를 직접 받고 드롭다운을 숨긴다.
  // 미제공 시 기존 동작 — 드롭다운으로 팀을 고른다.
  teamId?: string;
  teams?: TeamOption[];
  allTeams?: TeamOption[];
  models: ModelListItem[];
  /** 자체 저장 버튼 숨김 — 부모의 통합 저장이 ref.save() 를 호출한다. */
  hideActions?: boolean;
  /** 헤더(팀 선택/배지) 숨김 — PolicySection 헤더가 대신 보여준다. */
  bare?: boolean;
  /** 부모 저장 진행 중 — 편집을 잠근다. */
  disabled?: boolean;
  onDirtyChange?: (_dirty: boolean) => void;
  /** 저장된 정책 요약을 부모(섹션 헤더 배지)에 보고한다. */
  onSummaryChange?: (_summary: PolicySummary) => void;
}

export const TeamModelPermissionPanel = forwardRef<TeamModelPermissionHandle, TeamModelPermissionPanelProps>(
  function TeamModelPermissionPanel({ teamId, teams = [], allTeams, models, hideActions, bare, disabled, onDirtyChange, onSummaryChange }, ref) {
  const t = useTranslations('models');
  const { toast } = useToast();
  const [isPending, startTransition] = useTransition();
  const [pickedTeamId, setPickedTeamId] = useState('');
  const selectedTeamId = teamId ?? pickedTeamId;
  // 체크 상태 — 앱 접근 패널과 같은 표현: 무제한 = 전체 체크, 부분 체크 = 화이트리스트.
  // 저장은 항상 raw 목록으로 환산한다(전체 체크 → [] = 무제한).
  const [selected, setSelected] = useState<string[]>([]);
  // 저장된 raw whitelist — [] = 무제한(전체 허용). dirty 비교·배지 요약의 기준.
  const [loadedAliases, setLoadedAliases] = useState<string[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [showInactive, setShowInactive] = useState(false);

  const visibleTeams = showInactive && allTeams ? allTeams : teams;

  const activeModels = models.filter((m) => m.is_active);
  const activeAliases = useMemo(
    () => models.filter((m) => m.is_active).map((m) => m.alias),
    [models],
  );
  const activeSet = useMemo(() => new Set(activeAliases), [activeAliases]);
  // JSON.stringify — join('') 은 ["ab","c"] 와 ["a","bc"] 를 구분 못한다.
  const allAliasKey = JSON.stringify(activeAliases);
  // 카탈로그가 아직 안 왔거나 로드 실패면 빈 배열 — 이 상태로 저장하면
  // toSaved 가 무조건 [] 를 반환해 팀 제한이 무제한으로 지워진다(F1).
  const catalogReady = activeAliases.length > 0;
  const [loadFailed, setLoadFailed] = useState(false);

  /** 체크 상태 → 저장용 raw 목록. 카탈로그 전부 체크 = 무제한 → [].
      카탈로그 밖(비활성·삭제된) alias 는 저장 목록에서 제외한다 —
      개수 비교(sel.length>=N)로는 저장 목록에 stale alias 가 섞일 때
      로드 직후부터 phantom dirty + 저장 시 [] 로 덮는 버그가 생긴다(F2). */
  const toSaved = (sel: string[]) => {
    const inCatalog = sel.filter((a) => activeSet.has(a));
    return inCatalog.length >= activeAliases.length ? [] : [...inCatalog].sort();
  };

  useEffect(() => {
    let cancelled = false;
    // 팀 전환 즉시 이전 팀의 목록을 지운다 — loaded 가 true 인 채로 팀 A의
    // aliases 가 남으면 그 사이 토글·저장이 팀 B에 쓰인다.
    setSelected([]);
    setLoadedAliases([]);
    setLoaded(false);
    setLoadFailed(false);
    if (!selectedTeamId) return;
    startTransition(async () => {
      const result = await getTeamAllowedModelsAction(selectedTeamId);
      // 빠른 A→B→A 전환에서 늦게 돌아온 응답이 새 팀 상태를 덮지 않게 한다.
      if (cancelled) return;
      if (result.success) {
        // 카탈로그 밖(비활성·삭제) alias 는 정규화 — 다음 저장에서 자연 정리되고
        // 로드 직후 phantom dirty 가 생기지 않는다.
        const saved = result.data.model_aliases.filter((a) => activeSet.has(a));
        setLoadedAliases(saved);
        setSelected(saved.length > 0 ? saved : activeAliases);
        setLoaded(true);
      } else {
        // 실패를 성공으로 처리하면 !loaded 가드가 절대 안 걸리고 헤더에
        // "제한 없음" 이 오표기된다(F3) — 저장은 가드가 차단한다.
        setLoadFailed(true);
        toast({ type: 'error', message: t('loadFailed'), auto_dismiss_ms: 5000 });
      }
    });
    return () => {
      cancelled = true;
    };
    // allAliasKey — activeAliases 내용이 바뀌면(모델 카탈로그 갱신) 재로드.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedTeamId, allAliasKey]);

  const toggleModel = (alias: string) => {
    setSelected(prev =>
      prev.includes(alias) ? prev.filter(a => a !== alias) : [...prev, alias]
    );
  };

  // 순서 무관 비교 — 토글로 순서가 바뀌어도 dirty 가 아니다.
  const dirty =
    loaded &&
    toSaved(selected).join(',') !== [...loadedAliases].sort().join(',');

  useEffect(() => {
    onDirtyChange?.(dirty);
  }, [dirty, onDirtyChange]);

  // 헤더 배지는 **저장된** 상태만 보여준다(수정됨 마커는 dirty 가 담당).
  useEffect(() => {
    onSummaryChange?.({
      loaded,
      restricted: loadedAliases.length > 0,
      count: loadedAliases.length,
    });
  }, [loaded, loadedAliases, onSummaryChange]);

  const save = async (): Promise<boolean> => {
    if (!selectedTeamId) return false;
    if (!loaded || !catalogReady) {
      // 부모의 통합 저장이 이 경로를 친다 — 조용히 false 만 반환하면 앞 섹션은
      // 저장됐는데 왜 멈췄는지 사용자가 알 수 없다.
      toast({ type: 'error', message: t('saveNotReady'), auto_dismiss_ms: 4000 });
      return false;
    }
    // 0개 체크는 전면 거부처럼 보이지만 이 표현으로는 전면 거부를 저장할 수 없다
    // ([] = 제한 없음). 모호한 상태는 막고, 무제한은 "전체 선택 후 저장"으로
    // 표현하게 한다 — 앱 접근 패널의 0체크=상속과는 다른 규칙이다.
    if (selected.length === 0) {
      toast({ type: 'error', message: t('minOneModel'), auto_dismiss_ms: 3000 });
      return false;
    }
    // 전체 체크 → [] — 백엔드 replace-all 은 빈 목록을 "무제한 복귀"로 저장한다
    // (staged 제한 해제 — 별도 즉시 삭제 버튼을 두지 않는다).
    const result = await setTeamAllowedModelsAction(selectedTeamId, toSaved(selected));
    if (result.success) {
      const saved = result.data.model_aliases;
      setLoadedAliases(saved);
      setSelected(saved.length > 0 ? saved : activeAliases);
      if (!hideActions) {
        toast({ type: 'success', message: t('teamPermissionSaved'), auto_dismiss_ms: 3000 });
      }
      return true;
    }
    toast({ type: 'error', message: result.error, auto_dismiss_ms: 5000 });
    return false;
  };

  const revert = () => {
    setSelected(loadedAliases.length > 0 ? loadedAliases : activeAliases);
  };

  useImperativeHandle(ref, () => ({ save, revert }));

  const handleSave = () => {
    startTransition(async () => {
      await save();
    });
  };

  return (
    <div className={bare ? '' : 'space-y-4 glass rounded-apple p-4'}>
      {!bare && (
      <div className="flex items-center gap-4">
        {!teamId && (
          <>
            <label className="text-sm font-medium">{t('selectTeam')}</label>
            <select
              value={pickedTeamId}
              onChange={e => setPickedTeamId(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-1.5 text-sm"
            >
              <option value="">{t('selectTeamPlaceholder')}</option>
              {visibleTeams.map(t => (
                <option key={t.id} value={t.id}>
                  {t.department_name ? `${t.name} (${t.department_name})` : t.name}
                </option>
              ))}
            </select>
          </>
        )}
        {!teamId && allTeams && allTeams.length > teams.length && (
          <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
            <input
              type="checkbox"
              checked={showInactive}
              onChange={e => setShowInactive(e.target.checked)}
              className="h-3.5 w-3.5 rounded border-gray-300"
            />
            {t('includeInactiveTeams')}
          </label>
        )}
        {selectedTeamId && loaded && (
          loadedAliases.length > 0 ? (
            <span className="badge badge-amber">{t('restrictionApplied')}</span>
          ) : (
            <span className="badge badge-teal">{t('noRestriction')}</span>
          )
        )}
      </div>
      )}

      {selectedTeamId && loaded && !catalogReady && (
        <div className="text-xs text-muted-foreground py-1">{t('noActiveModels')}</div>
      )}

      {selectedTeamId && loaded && catalogReady && (
        <>
          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <span className="text-sm text-muted-foreground">
                {t('selectAllowedModels')}
                <span className="ml-2 text-xs font-medium text-foreground tabular-nums">
                  {t('selectedCount', { count: selected.length, total: activeAliases.length })}
                </span>
              </span>
              <div className="flex gap-2">
                <button
                  type="button"
                  onClick={() => setSelected(activeAliases)}
                  disabled={disabled}
                  className="text-xs text-primary hover:underline disabled:opacity-50"
                >
                  {t('selectAll')}
                </button>
                <button
                  type="button"
                  onClick={() => setSelected([])}
                  disabled={disabled}
                  className="text-xs text-muted-foreground hover:underline disabled:opacity-50"
                >
                  {t('clearAll')}
                </button>
              </div>
            </div>
            <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 md:grid-cols-4">
              {activeModels.map(m => (
                <label key={m.alias} className="flex items-center gap-2 rounded-md border p-2 cursor-pointer hover:bg-muted/50">
                  <input
                    type="checkbox"
                    checked={selected.includes(m.alias)}
                    onChange={() => toggleModel(m.alias)}
                    disabled={disabled}
                    className="h-4 w-4 rounded border-gray-300 disabled:opacity-50"
                  />
                  {/* 유저 패널(userModels)과 같은 라벨 — display_name 우선, 없으면 alias. */}
                  <span className="text-sm">{m.display_name || m.alias}</span>
                </label>
              ))}
            </div>
            {/* 0개 체크는 저장 불가 — "전부 차단" 으로 보이지만 저장값은 무제한과
                구분이 없는 모호한 상태다. 전체 허용은 모두 선택 후 저장한다. */}
            {selected.length === 0 && (
              <p className="text-xs text-amber-600 pt-1">{t('emptySaveHint')}</p>
            )}
          </div>

          {!hideActions && (
            <div className="flex items-center gap-3 pt-2 border-t">
              <SpinnerButton
                onClick={handleSave}
                isLoading={isPending}
                className="bg-primary text-primary-foreground hover:bg-primary/90 px-4 py-2 rounded-md text-sm font-medium"
              >
                {t('savePermission')}
              </SpinnerButton>
            </div>
          )}
        </>
      )}

      {selectedTeamId && !loaded && loadFailed && (
        <div className="text-xs text-destructive py-1">{t('loadFailed')}</div>
      )}

      {selectedTeamId && !loaded && !loadFailed && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground py-4">
          <div className="h-4 w-4 animate-spin rounded-full border-2 border-primary border-t-transparent" />
          {t('loadingText')}
        </div>
      )}
    </div>
  );
});
