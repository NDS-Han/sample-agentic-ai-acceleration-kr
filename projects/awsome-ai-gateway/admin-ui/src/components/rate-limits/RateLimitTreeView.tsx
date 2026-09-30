'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useEffect, useRef, useState } from 'react';
import { useTranslations } from 'next-intl';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitTree } from './RateLimitTree';
import { RateLimitConfigPanel } from './RateLimitConfigPanel';
import { ConfirmDialog } from '@/components/common/ConfirmDialog';

interface RateLimitTreeViewProps {
  nodes: RateLimitTreeNode[];
}

const EXPANDED_NODES_STORAGE_KEY = 'rate-limits:tree:expandedNodes';

function collectDefaultExpanded(nodes: RateLimitTreeNode[]): Set<string> {
  // 기본적으로 GLOBAL 만 펼침. TEAM 은 접혀 있어서 USER 리스트 숨김.
  const ids = new Set<string>();
  for (const n of nodes) {
    if (n.scope === 'GLOBAL') ids.add(n.id);
  }
  return ids;
}

export function RateLimitTreeView({ nodes }: RateLimitTreeViewProps) {
  const t = useTranslations('rateLimits');
  const [selectedNode, setSelectedNode] = useState<RateLimitTreeNode | null>(null);
  // 미저장 편집 중 노드 전환 시도 — 확인 후 전환한다(예전엔 입력이 무통고 소실됐다).
  const [panelDirty, setPanelDirty] = useState(false);
  const [pendingNode, setPendingNode] = useState<RateLimitTreeNode | null>(null);
  const [expandedNodes, setExpandedNodes] = useState<Set<string>>(() =>
    collectDefaultExpanded(nodes)
  );
  const [showInactive, setShowInactive] = useState(false);
  const hasMountedRef = useRef(false);

  const hasInactive = nodes.some(n => n.is_active === false);
  const filteredNodes = showInactive
    ? nodes
    : nodes.filter(n => n.is_active !== false).map(n => ({
        ...n,
        children: n.children.filter(c => c.is_active !== false),
      }));

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
  }, []);

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

  const handleToggle = (id: string) => {
    setExpandedNodes((prev) => {
      if (prev.has(id)) {
        return new Set([...prev].filter((x) => x !== id));
      }
      return new Set([...prev, id]);
    });
  };

  /** 같은 노드 재클릭이 아닌 전환 시, dirty 폼이 있으면 확인을 거친다. */
  const handleSelect = (node: RateLimitTreeNode) => {
    if (panelDirty && node.id !== selectedNode?.id) {
      setPendingNode(node);
      return;
    }
    setSelectedNode(node);
  };

  const confirmSwitch = () => {
    if (pendingNode) setSelectedNode(pendingNode);
    setPendingNode(null);
  };

  return (
    <div className="space-y-3">
      {hasInactive && (
        <label className="flex items-center gap-1.5 cursor-pointer text-xs text-muted-foreground">
          <input
            type="checkbox"
            checked={showInactive}
            onChange={e => setShowInactive(e.target.checked)}
            className="h-3.5 w-3.5 rounded border-gray-300"
          />
          {t('includeInactive')}
        </label>
      )}
      <div className="flex gap-0 border rounded-lg overflow-hidden min-h-[600px]">
      <div className="w-72 border-r overflow-y-auto">
        <RateLimitTree
          nodes={filteredNodes}
          selectedNodeId={selectedNode?.id ?? null}
          expandedNodes={expandedNodes}
          onSelect={handleSelect}
          onToggle={handleToggle}
        />
      </div>
      <div className="flex-1 p-6">
        <RateLimitConfigPanel node={selectedNode} onDirtyChange={setPanelDirty} />
      </div>
      </div>
      <ConfirmDialog
        isOpen={pendingNode !== null}
        onClose={() => setPendingNode(null)}
        onConfirm={confirmSwitch}
        title={t('discardChangesTitle')}
        message={t('discardChangesMessage')}
        confirmLabel={t('discardChangesConfirm')}
        isDestructive
      />
    </div>
  );
}