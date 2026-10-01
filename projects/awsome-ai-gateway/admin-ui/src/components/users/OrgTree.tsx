// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Building2, FolderOpen, Folder, Users, User, ChevronRight, ChevronDown } from 'lucide-react';
import type { OrgTreeNode } from '@/types/entities';
import type { OrgNodeType } from '@/types/enums';

interface OrgTreeProps {
  node: OrgTreeNode;
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (node: OrgTreeNode) => void;
  onToggle: (id: string) => void;
  depth?: number;
  /** 멤버 0인 팀 노드 옆에 붙는 배지 텍스트(빈 팀 표시 토글 ON 때 보임). */
  emptyTeamLabel?: string;
}

function NodeIcon({ type, isExpanded }: { type: OrgNodeType; isExpanded: boolean }) {
  switch (type) {
    case 'ORGANIZATION':
      return <Building2 size={14} className="flex-shrink-0" aria-hidden="true" />;
    case 'DEPARTMENT':
      return isExpanded
        ? <FolderOpen size={14} className="flex-shrink-0" aria-hidden="true" />
        : <Folder size={14} className="flex-shrink-0" aria-hidden="true" />;
    case 'TEAM':
      return <Users size={14} className="flex-shrink-0" aria-hidden="true" />;
    case 'USER':
      return <User size={14} className="flex-shrink-0" aria-hidden="true" />;
    default:
      return null;
  }
}

const EXPANDABLE_TYPES: OrgNodeType[] = ['ORGANIZATION', 'DEPARTMENT', 'TEAM'];

/** 현재 보이는 treeitem 버튼 목록 — 방향키 이동의 순서표. */
function visibleTreeButtons(): HTMLElement[] {
  return Array.from(
    document.querySelectorAll('[role="tree"] [role="treeitem"] > button'),
  );
}

export function OrgTree({
  node,
  selectedNodeId,
  expandedNodes,
  onSelect,
  onToggle,
  depth = 0,
  emptyTeamLabel,
}: OrgTreeProps) {
  const isSelected = selectedNodeId === node.id;
  const hasChildren = node.children && node.children.length > 0;
  const isExpandable = EXPANDABLE_TYPES.includes(node.type) && hasChildren;
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
          // 부모 treeitem 의 버튼으로 이동 — 직계 부모 group → 그 부모 treeitem.
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

  const item = (
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
          'w-full flex items-center gap-2 px-3 py-2 text-sm text-left hover:bg-accent/50 rounded-sm transition-colors',
          isSelected ? 'bg-accent text-accent-foreground' : '',
        ]
          .filter(Boolean)
          .join(' ')}
        style={{ paddingLeft: `${12 + depth * 16}px` }}
      >
        {/* 펼치기/접기 chevron */}
        {isExpandable ? (
          isExpanded ? (
            <ChevronDown size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          ) : (
            <ChevronRight size={12} className="flex-shrink-0 text-muted-foreground" aria-hidden="true" />
          )
        ) : (
          <span className="w-3 flex-shrink-0" aria-hidden="true" />
        )}

        <NodeIcon type={node.type} isExpanded={isExpanded} />
        <span className="flex-1 truncate">{node.name}</span>
        {emptyTeamLabel && node.type === 'TEAM' && node.meta.member_count === 0 && (
          <span className="text-[10px] text-muted-foreground flex-shrink-0">
            {emptyTeamLabel}
          </span>
        )}
      </button>
    </div>
  );

  const childrenGroup = isExpanded && hasChildren ? (
    <div role="group">
      {node.children.map((child) => (
        <OrgTree
          key={child.id}
          node={child}
          selectedNodeId={selectedNodeId}
          expandedNodes={expandedNodes}
          onSelect={onSelect}
          onToggle={onToggle}
          depth={depth + 1}
          emptyTeamLabel={emptyTeamLabel}
        />
      ))}
    </div>
  ) : null;

  // 최상위 호출만 role="tree" 컨테이너를 연다 — 재귀 자식은 group 안의 treeitem.
  if (depth === 0) {
    return (
      <div role="tree" aria-label="organization tree">
        {item}
        {childrenGroup}
      </div>
    );
  }

  return (
    <>
      {item}
      {childrenGroup}
    </>
  );
}
