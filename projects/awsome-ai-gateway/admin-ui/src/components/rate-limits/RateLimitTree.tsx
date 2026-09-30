'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Globe, Users, User, ChevronRight, ChevronDown } from 'lucide-react';
import { useTranslations } from 'next-intl';
import type { RateLimitTreeNode } from '@/types/entities';
import type { RateLimitScope } from '@/types/enums';

interface RateLimitTreeProps {
  nodes: RateLimitTreeNode[];
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: RateLimitTreeNode) => void;
  onToggle: (id: string) => void;
  depth?: number;
}

function NodeIcon({ scope }: { scope: RateLimitScope }) {
  if (scope === 'GLOBAL') return <Globe size={14} className="flex-shrink-0" aria-hidden="true" />;
  if (scope === 'TEAM') return <Users size={14} className="flex-shrink-0" aria-hidden="true" />;
  return <User size={14} className="flex-shrink-0" aria-hidden="true" />;
}

interface TreeNodeProps {
  node: RateLimitTreeNode;
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: RateLimitTreeNode) => void;
  onToggle: (id: string) => void;
  depth: number;
}

/** 현재 보이는 treeitem 버튼 목록 — 방향키 이동의 순서표. */
function visibleTreeButtons(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll('[role="tree"] [role="treeitem"] > button'),
  );
}

function TreeNode({
  node,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth,
}: TreeNodeProps) {
  const t = useTranslations('rateLimits');
  const isSelected = selectedNodeId === node.id;
  const hasChildren = Boolean(node.children && node.children.length > 0);
  // USER 는 leaf. GLOBAL/TEAM 만 expandable.
  const isExpandable = node.scope !== 'USER' && hasChildren;
  const isExpanded = expandedNodes.has(node.id);

  const handleClick = () => {
    onSelect(node);
    if (isExpandable) {
      onToggle(node.id);
    }
  };

  // ARIA treeview 키보드 패턴 — ↑/↓ 보이는 항목 이동, → 펼치기/첫 자식, ← 접기/부모.
  const handleKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    const buttons = visibleTreeButtons();
    const idx = buttons.indexOf(e.currentTarget);
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        buttons[idx + 1]?.focus();
        break;
      case 'ArrowUp':
        e.preventDefault();
        buttons[idx - 1]?.focus();
        break;
      case 'ArrowRight':
        e.preventDefault();
        if (isExpandable && !isExpanded) {
          onToggle(node.id);
        } else {
          buttons[idx + 1]?.focus();
        }
        break;
      case 'ArrowLeft':
        e.preventDefault();
        if (isExpandable && isExpanded) {
          onToggle(node.id);
        } else {
          const parent = e.currentTarget
            .closest('[role="treeitem"]')
            ?.parentElement?.closest('[role="treeitem"]');
          parent?.querySelector<HTMLElement>(':scope > button')?.focus();
        }
        break;
      case 'Enter':
      case ' ':
        e.preventDefault();
        handleClick();
        break;
    }
  };

  return (
    <>
      <div
        role="treeitem"
        aria-expanded={isExpandable ? isExpanded : undefined}
        aria-selected={isSelected}
        aria-level={depth + 1}
      >
        <button
          onClick={handleClick}
          onKeyDown={handleKeyDown}
          className={[
            'w-full flex items-center gap-2 py-2 text-sm hover:bg-accent/50 transition-colors text-left',
            isSelected ? 'bg-accent text-accent-foreground' : '',
          ]
            .filter(Boolean)
            .join(' ')}
          style={{ paddingLeft: `${12 + depth * 16}px`, paddingRight: '12px' }}
        >
          {isExpandable ? (
            isExpanded ? (
              <ChevronDown size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
            ) : (
              <ChevronRight size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
            )
          ) : (
            <span className="w-3 flex-shrink-0" aria-hidden="true" />
          )}

          <NodeIcon scope={node.scope} />
          <span className="flex-1 truncate">{node.label}</span>
          <span className="text-xs text-muted-foreground flex-shrink-0">
            {node.inherited_from
              ? t('inherited')
              : node.config
              ? `${node.config.rpm ?? '∞'}rpm`
              : '-'}
          </span>
        </button>
      </div>

      {isExpanded && hasChildren && (
        <div role="group">
          {node.children.map((child) => (
            <TreeNode
              key={child.id}
              node={child}
              selectedNodeId={selectedNodeId}
              expandedNodes={expandedNodes}
              onSelect={onSelect}
              onToggle={onToggle}
              depth={depth + 1}
            />
          ))}
        </div>
      )}
    </>
  );
}

export function RateLimitTree({
  nodes,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth = 0,
}: RateLimitTreeProps) {
  const t = useTranslations('rateLimits');
  if (nodes.length === 0) {
    return (
      <p className="p-4 text-sm text-muted-foreground">{t('noConfigData')}</p>
    );
  }

  return (
    <div className="py-1" role="tree" aria-label={t('title')}>
      {nodes.map((node) => (
        <TreeNode
          key={node.id}
          node={node}
          selectedNodeId={selectedNodeId}
          expandedNodes={expandedNodes}
          onSelect={onSelect}
          onToggle={onToggle}
          depth={depth}
        />
      ))}
    </div>
  );
}
