'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useMemo, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { OrgTreeNode, UserSearchItem } from '@/types/entities';
import { OrgTree } from './OrgTree';
import { OrgDetailPanel } from './OrgDetailPanel';
import { OrgSearchBox } from './OrgSearchBox';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';
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
    const found = findNodeById(root, selectedId);
    // 검색으로 선택한 USER 는 트리 노드보다 메타(team_name 등)가 충실한 합성
    // 노드를 우선한다. TEAM/DEPARTMENT 는 항상 최신 root 노드를 쓴다.
    if (found?.type === 'USER' && syntheticNode?.id === selectedId) {
      return syntheticNode;
    }
    return found ?? (syntheticNode?.id === selectedId ? syntheticNode : null);
  }, [root, selectedId, syntheticNode]);

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
    if (deepId) {
      const found = findNodeById(root, deepId);
      if (found) {
        setSelectedId(found.id);
        const path = findNodeExpandPath(root, found.id) ?? [];
        // 팀이면 자신도 펼친다 — 트리 클릭과 같은 결과(멤버 노출)가 돼야 한다.
        const toExpand = found.type === 'TEAM' ? [...path, found.id] : path;
        if (toExpand.length > 0) {
          setExpandedNodes((prev) => new Set([...prev, ...toExpand]));
        }
      }
    }
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

  // Cognito 동기화 등으로 root 가 갱신됐는데 선택 노드가 사라졌으면 선택을 해제한다.
  useEffect(() => {
    if (!selectedId || syntheticNode?.id === selectedId) return;
    if (root && !findNodeById(root, selectedId)) {
      setSelectedId(null);
      syncUrl(null);
    }
  }, [root, selectedId, syntheticNode]);

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
      const path = findTeamExpandPath(root, user.team_id);
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

  return (
    <div className="flex flex-col gap-3">
      <OrgSearchBox
        root={root}
        onSelectOrgNode={handleSelectOrgNode}
        onSelectUser={handleSelectUser}
      />
      <div className="flex gap-0 border rounded-lg overflow-hidden min-h-[600px]">
      <div className="w-72 border-r overflow-y-auto">
        {root ? (
          <OrgTree
            node={root}
            selectedNodeId={selectedId}
            expandedNodes={expandedNodes}
            onSelect={(node) => {
              if (node.id !== selectedId) requestSelect(() => selectTreeNode(node));
            }}
            onToggle={handleToggle}
          />
        ) : (
          <p className="p-4 text-muted-foreground text-sm">{t('noOrgData')}</p>
        )}
      </div>
      <div className="flex-1 p-6">
        <OrgDetailPanel node={selectedNode} onDirtyChange={setPanelDirty} />
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
