'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { OrgTreeNode, UserSearchItem } from '@/types/entities';
import { OrgTree } from './OrgTree';
import { OrgDetailPanel } from './OrgDetailPanel';
import { OrgSearchBox } from './OrgSearchBox';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
import { useToast } from '@/components/common/ToastProvider';
import { getOrgTreeAction } from '@/lib/actions/users';
import {
  findNodeById,
  findNodeExpandPath,
  findTeamExpandPath,
  type OrgMatch,
} from '@/lib/utils/orgSearch';

interface OrgTreeViewProps {
  root: OrgTreeNode | null;
}

const EXPANDED_NODES_STORAGE_KEY = 'users:orgtree:expandedNodes';

export function OrgTreeView({ root }: OrgTreeViewProps) {
  const t = useTranslations('users');
  const { toast } = useToast();
  // "빈 팀 표시" 토글이 켜지면 include_empty 트리로 교체한다 — 라우트
  // 네비게이션 없이 서버 액션으로 재조회해 선택·dirty 상태를 보존한다.
  // prop root 는 서버가 준 기본 트리(빈 팀 제외)라 OFF 복귀 시 재조회 불필요.
  const [treeOverride, setTreeOverride] = useState<OrgTreeNode | null>(null);
  // treeOverride 가 include_empty 결과인지 — 저장 후 재조회(onSaved)가 기본 트리를
  // override 에 넣었을 때 "빈 팀 표시" ON 이 그걸 재사용해 빈 팀이 안 뜨는 걸 막는다.
  const [overrideHasEmpty, setOverrideHasEmpty] = useState(false);
  const [showEmptyTeams, setShowEmptyTeams] = useState(false);
  const [treeLoading, setTreeLoading] = useState(false);
  const effectiveRoot = treeOverride ?? root;
  // 선택은 id 가 정본 — 노드 객체는 매 렌더 root 에서 다시 찾는다.
  // router.refresh() 로 root 가 갱신돼도 선택이 유지되고 표시는 최신 데이터다
  // (예전엔 선택된 node 객체가 stale 해 리더 지정 후 목록이 안 바뀌었다).
  const [selectedId, setSelectedId] = useState<string | null>(null);
  // 검색 결과로만 존재하는 USER 노드(팀 미배정·멤버 0명 팀 등 root 에 없는 사용자).
  const [syntheticNode, setSyntheticNode] = useState<OrgTreeNode | null>(null);
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(new Set());
  // 미저장 편집 중 노드 전환 시도 — 확인 후 전환한다(RateLimitTreeView 와 같은 규칙).
  const [panelDirty, setPanelDirty] = useState(false);
  const [pendingSelect, setPendingSelect] = useState<(() => void) | null>(null);
  const hasMountedRef = useRef(false);

  const selectedNode = useMemo(() => {
    if (!selectedId) return null;
    const found = findNodeById(effectiveRoot, selectedId);
    // 검색으로 선택한 USER 는 트리 노드보다 메타(team_name 등)가 충실한 합성
    // 노드를 우선한다. TEAM/DEPARTMENT 는 항상 최신 root 노드를 쓴다.
    if (found?.type === 'USER' && syntheticNode?.id === selectedId) {
      return syntheticNode;
    }
    return found ?? (syntheticNode?.id === selectedId ? syntheticNode : null);
  }, [effectiveRoot, selectedId, syntheticNode]);

  // sessionStorage에서 펼침 상태 복원 + ?node= 딥링크 복원 (mount 1회)
  useEffect(() => {
    try {
      const raw = sessionStorage.getItem(EXPANDED_NODES_STORAGE_KEY);
      if (raw) {
        const ids = JSON.parse(raw) as unknown;
        if (Array.isArray(ids) && ids.every((x) => typeof x === 'string')) {
          setExpandedNodes(new Set(ids as string[]));
        }
      }
    } catch {
      // 손상/비활성 storage 무시
    }
    // 딥링크 — 트리에 실제로 있는 노드만 복원한다(검색 전용 합성 노드는 불가).
    const deepId = new URLSearchParams(window.location.search).get('node');
    // 딥링크가 없으면 루트를 펼치고 선택한다 — 우측 패널이 빈 상태로 시작해
    // 무엇을 해야 할지 보이지 않던 데드엔드(리뷰 TOP-2) 해소. 선택은 id 기반이고
    // 지속되지 않으므로 매 진입 시 루트 조직 패널이 열리는 것이 정상 동작이다.
    if (!deepId) {
      if (root) {
        setExpandedNodes((prev) => new Set([...prev, root.id]));
        setSelectedId(root.id);
      }
      return;
    }
    const applyDeepLink = (tree: OrgTreeNode | null) => {
      const found = findNodeById(tree, deepId);
      if (!found) return false;
      setSelectedId(found.id);
      const path = findNodeExpandPath(tree, found.id) ?? [];
      // 팀이면 자신도 펼친다 — 트리 클릭과 같은 결과(멤버 노출)가 돼야 한다.
      const toExpand = found.type === 'TEAM' ? [...path, found.id] : path;
      if (toExpand.length > 0) {
        setExpandedNodes((prev) => new Set([...prev, ...toExpand]));
      }
      return true;
    };
    if (applyDeepLink(root)) return;
    // 기본 트리에 없으면 빈 팀일 수 있다 — include_empty 로 한 번 더 조회해
    // 딥링크가 멤버 0인 팀에서도 동작하게 한다.
    void (async () => {
      const r = await getOrgTreeAction(true);
      if (!r.success || !r.data) return;
      if (applyDeepLink(r.data)) {
        setTreeOverride(r.data);
        setOverrideHasEmpty(true);
        setShowEmptyTeams(true);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount 1회 복원 전용
  }, []);

  // 펼침 상태 변경 시 persist (초기 빈 Set으로 덮어쓰지 않도록 첫 호출 skip)
  useEffect(() => {
    if (!hasMountedRef.current) {
      hasMountedRef.current = true;
      return;
    }
    try {
      sessionStorage.setItem(
        EXPANDED_NODES_STORAGE_KEY,
        JSON.stringify([...expandedNodes])
      );
    } catch {
      // quota/비활성 storage 무시
    }
  }, [expandedNodes]);

  // 노드 전환 직후 dirty 를 비운다 — 새 패널이 마운트되며 자신의 dirty 를 다시 보고한다.
  useEffect(() => {
    setPanelDirty(false);
  }, [selectedId]);

  // Cognito 동기화·빈 팀 토글 OFF 등으로 트리에서 선택 노드가 사라졌으면
  // 선택을 해제한다. dirty 폼이 있으면 토글 자체가 가드(아래 handleToggleEmpty)
  // 를 거치므로 이 경로는 비dirty 전환만 처리한다.
  useEffect(() => {
    if (!selectedId || syntheticNode?.id === selectedId) return;
    if (effectiveRoot && !findNodeById(effectiveRoot, selectedId)) {
      setSelectedId(null);
      syncUrl(null);
    }
  }, [effectiveRoot, selectedId, syntheticNode]);

  /**
   * 선택을 ?node= 에 기록한다. router.push 를 쓰지 않는다 — 서버 라운드트립으로
   * 트리 재조회 + remount 가 일어나 패널 상태(dirty 포함)가 날아가기 때문.
   */
  const syncUrl = (id: string | null) => {
    const url = id
      ? `${window.location.pathname}?node=${encodeURIComponent(id)}`
      : window.location.pathname;
    window.history.replaceState(null, '', url);
  };

  /** dirty 폼이 있으면 ConfirmDialog 를 거쳐 전환한다. */
  const requestSelect = (action: () => void) => {
    if (panelDirty) {
      setPendingSelect(() => action);
      return;
    }
    action();
  };

  const selectTreeNode = (node: OrgTreeNode) => {
    setSyntheticNode(null);
    setSelectedId(node.id);
    syncUrl(node.id);
  };

  // ── 검색 결과 선택 ──────────────────────────────────────────────────────────

  /** 부서/팀: 조상 경로를 펼쳐 트리에 드러낸 뒤 선택한다. */
  const handleSelectOrgNode = (match: OrgMatch) => {
    requestSelect(() => {
      const { node, ancestorIds } = match;
      // 팀이면 자신도 펼친다 — 멤버를 바로 보여주는 것이 클릭했을 때와 같은 결과다.
      const toExpand = node.type === 'TEAM' ? [...ancestorIds, node.id] : ancestorIds;
      setExpandedNodes((prev) => new Set([...prev, ...toExpand]));
      selectTreeNode(node);
    });
  };

  /**
   * 사용자: 소속 팀 경로를 펼쳐 트리에서 위치를 드러내고, 상세 패널은 검색 결과로
   * 즉시 채운다.
   *
   * ⚠️ 검색 결과 항목으로 USER 노드를 **합성**한다. 트리에서 같은 사용자를 찾아 쓰지
   *    않는 이유는 두 가지다: (1) 팀이 트리에 없을 수 있고(멤버 0명 팀은 서버가
   *    숨긴다), (2) 팀 미배정 사용자는 트리에 자리가 없다. 두 경우에도 상세는 보여야
   *    한다. 합성 노드의 id 는 실제 노드와 같으므로 트리 하이라이트도 맞는다.
   */
  const handleSelectUser = (user: UserSearchItem) => {
    requestSelect(() => {
      setSyntheticNode({
        id: user.id,
        name: user.display_name,
        type: 'USER',
        children: [],
        meta: {
          member_count: null,
          team_count: null,
          leader_name: null,
          leader_user_id: null,
          email: user.email,
          role: user.role,
          team_name: user.team_name,
        },
      });
      setSelectedId(user.id);
      syncUrl(user.id);

      if (!user.team_id) return; // 팀 미배정 — 트리에 드러낼 자리가 없다
      const path = findTeamExpandPath(effectiveRoot, user.team_id);
      if (!path) return; // 팀이 트리에 없다(멤버 0명 등) — 상세 패널만
      setExpandedNodes((prev) => new Set([...prev, ...path]));
    });
  };

  const handleToggle = (id: string) => {
    setExpandedNodes((prev) => {
      if (prev.has(id)) {
        return new Set([...prev].filter((x) => x !== id));
      }
      return new Set([...prev, id]);
    });
  };

  // ── 빈 팀 표시 토글 ─────────────────────────────────────────────────────────
  // 멤버 0인 팀은 서버가 기본 트리에서 생략한다 — 신규 Cognito 팀에 멤버 배정 전
  // 정책을 미리 설정할 진입점이 필요해 ON 일 때 include_empty 트리로 교체한다.
  const applyShowEmpty = async (checked: boolean) => {
    setShowEmptyTeams(checked);
    if (!checked) {
      // 기본 트리로 복귀 — override 가 기본 트리면(onSaved 재조회) 그대로 쓰고,
      // include_empty 결과면 빈 팀이 남지 않게 버린다.
      if (overrideHasEmpty) setTreeOverride(null);
      setOverrideHasEmpty(false);
      return;
    }
    if (treeOverride && overrideHasEmpty) return; // 이미 가져온 트리 재사용
    setTreeLoading(true);
    const r = await getOrgTreeAction(true);
    setTreeLoading(false);
    if (r.success) {
      setTreeOverride(r.data);
      setOverrideHasEmpty(true);
    } else {
      setShowEmptyTeams(false);
      toast({ type: 'error', message: t('loadErrors.tree'), auto_dismiss_ms: 5000 });
    }
  };

  /**
   * 토글 OFF 시 선택된 빈 팀이 트리에서 사라진다 — dirty 폼이 있으면
   * 노드 전환과 같은 ConfirmDialog 가드를 거친다.
   */
  const handleToggleEmpty = (checked: boolean) => {
    if (!checked && panelDirty && selectedId && syntheticNode?.id !== selectedId) {
      const survives = !!findNodeById(root, selectedId);
      if (!survives) {
        setPendingSelect(() => () => {
          void applyShowEmpty(false);
        });
        return;
      }
    }
    void applyShowEmpty(checked);
  };

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center justify-between gap-3 flex-wrap">
        <OrgSearchBox
          root={effectiveRoot}
          onSelectOrgNode={handleSelectOrgNode}
          onSelectUser={handleSelectUser}
        />
        <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground select-none">
          <input
            type="checkbox"
            checked={showEmptyTeams}
            onChange={(e) => handleToggleEmpty(e.target.checked)}
            disabled={treeLoading}
            className="h-3.5 w-3.5 rounded border-gray-300"
          />
          {t('showEmptyTeams')}
        </label>
      </div>
      <div className="flex gap-0 border rounded-lg overflow-hidden min-h-[600px]">
      <div className="w-72 border-r overflow-y-auto">
        {effectiveRoot ? (
          <OrgTree
            node={effectiveRoot}
            selectedNodeId={selectedId}
            expandedNodes={expandedNodes}
            onSelect={(node) => {
              if (node.id !== selectedId) requestSelect(() => selectTreeNode(node));
            }}
            onToggle={handleToggle}
            emptyTeamLabel={t('emptyTeamBadge')}
            customPolicyLabel={t('customBadge')}
            formatCustomCount={(n) => t('customCount', { count: n })}
          />
        ) : (
          <p className="p-4 text-muted-foreground text-sm">{t('noOrgData')}</p>
        )}
      </div>
      <div className="flex-1 p-6">
        <OrgDetailPanel
          node={selectedNode}
          onDirtyChange={setPanelDirty}
          orgId={effectiveRoot?.type === 'ORGANIZATION' ? effectiveRoot.id : undefined}
          onSaved={() => {
            // 저장으로 개별설정 점이 바뀔 수 있다 — 트리 메타를 재조회해 갱신.
            void (async () => {
              const r = await getOrgTreeAction(showEmptyTeams);
              if (r.success && r.data) {
                setTreeOverride(r.data);
                setOverrideHasEmpty(showEmptyTeams);
              }
            })();
          }}
        />
      </div>
      </div>
      <ConfirmDialog
        isOpen={pendingSelect !== null}
        onClose={() => setPendingSelect(null)}
        onConfirm={() => pendingSelect?.()}
        title={t('discardChangesTitle')}
        message={t('discardChangesMessage')}
        confirmLabel={t('discardChangesConfirm')}
        isDestructive
      />
    </div>
  );
}
