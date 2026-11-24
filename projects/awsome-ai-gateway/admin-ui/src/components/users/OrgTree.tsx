// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Building2, FolderOpen, Folder, Users, User, ChevronRight, ChevronDown } from 'lucide-react';
import type { OrgTreeNode } from '@/types/entities';
import type { OrgNodeType } from '@/types/enums';

interface OrgTreeProps {
  node: OrgTreeNode;
  selectedNodeId: string | null;
  expandedNodes: Set<string>;
  onSelect: (_node: OrgTreeNode) => void;
  onToggle: (_id: string) => void;
  /** 펼치기만(접지 않음) — 팀 선택 시 멤버를 드러낸다. */
  onExpand: (_id: string) => void;
  depth?: number;
  labels: {
    /** 멤버 0인 팀 배지(빈 팀 표시 ON 때만 보임). */
    emptyTeam: string;
    /** 개별 정책(앱/모델 override)을 가진 사용자 점의 설명. */
    customPolicy: string;
    leader: string;
    memberCount: (_n: number) => string;
    expand: (_name: string) => string;
    collapse: (_name: string) => string;
  };
}

function NodeIcon({ type, isExpanded }: { type: OrgNodeType; isExpanded: boolean }) {
  const cls = 'flex-shrink-0 text-muted-foreground';
  switch (type) {
    case 'ORGANIZATION':
      return <Building2 size={14} className={cls} aria-hidden="true" />;
    case 'DEPARTMENT':
      return isExpanded ? <FolderOpen size={14} className={cls} aria-hidden="true" /> : <Folder size={14} className={cls} aria-hidden="true" />;
    case 'TEAM':
      return <Users size={14} className={cls} aria-hidden="true" />;
    case 'USER':
      return <User size={14} className={cls} aria-hidden="true" />;
    default:
      return null;
  }
}

const EXPANDABLE_TYPES: OrgNodeType[] = ['ORGANIZATION', 'DEPARTMENT', 'TEAM'];

/** 현재 보이는 행 버튼 — 방향키 이동의 순서표(셰브런 버튼은 제외). */
function visibleRowButtons(): HTMLElement[] {
  return Array.from(document.querySelectorAll('[role="tree"] [data-tree-row]'));
}

/** 트리 한 행의 두 줄 라벨 — 사용자는 이름 + 이메일(이름이 없으면 이메일만), 팀은 짧은 이름 + 전체 이름. */
export function rowLabels(node: OrgTreeNode): { primary: string; secondary: string | null } {
  if (node.type === 'USER') {
    const email = node.meta.email ?? null;
    const hasName = !!node.name && node.name !== email;
    return { primary: hasName ? node.name : (email ?? node.name), secondary: hasName ? email : null };
  }
  if (node.type === 'TEAM') {
    const full = node.meta.team_display_name;
    return { primary: node.name, secondary: full && full !== node.name ? full : null };
  }
  return { primary: node.name, secondary: null };
}

/**
 * 조직 트리 — ARIA treeview.
 *  - 행 클릭/Enter/Space = **선택**. 팀은 선택하며 펼친다(접지는 않는다).
 *  - 셰브런 = 펼치기/접기만(선택 안 바뀜). 키보드는 →/← 가 같은 일을 한다.
 *  - ↑/↓ 보이는 행 이동, ← 는 접혀 있으면 부모로.
 */
export function OrgTree({ node, selectedNodeId, expandedNodes, onSelect, onToggle, onExpand, depth = 0, labels }: OrgTreeProps) {
  const isSelected = selectedNodeId === node.id;
  const hasChildren = node.children && node.children.length > 0;
  const isExpandable = EXPANDABLE_TYPES.includes(node.type) && hasChildren;
  const isExpanded = expandedNodes.has(node.id);
  const { primary, secondary } = rowLabels(node);
  const isLeader = node.type === 'USER' && node.meta.role === 'TEAM_LEADER';
  const count = node.type === 'TEAM' || node.type === 'DEPARTMENT' || node.type === 'ORGANIZATION' ? node.meta.member_count : null;

  const select = () => {
    onSelect(node);
    if (node.type === 'TEAM' && isExpandable && !isExpanded) onExpand(node.id);
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLButtonElement>) => {
    const rows = visibleRowButtons();
    const idx = rows.indexOf(e.currentTarget);
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault();
        rows[idx + 1]?.focus();
        break;
      case 'ArrowUp':
        e.preventDefault();
        rows[idx - 1]?.focus();
        break;
      case 'ArrowRight':
        e.preventDefault();
        if (isExpandable && !isExpanded) onToggle(node.id);
        else rows[idx + 1]?.focus();
        break;
      case 'ArrowLeft': {
        e.preventDefault();
        if (isExpandable && isExpanded) {
          onToggle(node.id);
        } else {
          const parent = e.currentTarget.closest('[role="treeitem"]')?.parentElement?.closest('[role="treeitem"]');
          parent?.querySelector<HTMLElement>(':scope > div > [data-tree-row]')?.focus();
        }
        break;
      }
      case 'Enter':
      case ' ':
        e.preventDefault();
        select();
        break;
    }
  };

  const title = secondary ? `${primary} · ${secondary}` : primary;

  const item = (
    <div role="treeitem" aria-expanded={isExpandable ? isExpanded : undefined} aria-selected={isSelected} aria-level={depth + 1}>
      <div
        className={[
          'group/row flex items-center gap-1 rounded-sm pr-2 transition-colors',
          isSelected ? 'bg-accent text-accent-foreground' : 'hover:bg-accent/50',
        ].join(' ')}
        style={{ paddingLeft: `${6 + depth * 16}px` }}
      >
        {isExpandable ? (
          <button
            type="button"
            tabIndex={-1}
            aria-label={isExpanded ? labels.collapse(primary) : labels.expand(primary)}
            onClick={() => onToggle(node.id)}
            className="flex h-6 w-6 flex-shrink-0 items-center justify-center rounded text-muted-foreground hover:bg-muted hover:text-foreground"
            data-tree-chevron
          >
            {isExpanded ? <ChevronDown size={13} aria-hidden="true" /> : <ChevronRight size={13} aria-hidden="true" />}
          </button>
        ) : (
          <span className="w-6 flex-shrink-0" aria-hidden="true" />
        )}
        <button
          type="button"
          data-tree-row
          onClick={select}
          onKeyDown={handleKeyDown}
          title={title}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-sm py-1.5 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <NodeIcon type={node.type} isExpanded={isExpanded} />
          <span className="min-w-0 flex-1">
            <span className={`block truncate ${node.type === 'USER' ? 'font-medium' : 'font-semibold'}`}>{primary}</span>
            {secondary && <span className="block truncate text-[11px] text-muted-foreground">{secondary}</span>}
          </span>
          {isLeader && <span className="badge badge-sky flex-shrink-0 whitespace-nowrap text-[10px]">{labels.leader}</span>}
          {node.type === 'USER' && node.meta.has_custom_policies === true && (
            <span
              className="h-1.5 w-1.5 flex-shrink-0 rounded-full bg-amber-500"
              title={labels.customPolicy}
              role="img"
              aria-label={labels.customPolicy}
            />
          )}
          {node.type === 'TEAM' && node.meta.member_count === 0 ? (
            <span className="flex-shrink-0 text-[10px] text-muted-foreground">{labels.emptyTeam}</span>
          ) : (
            count != null &&
            node.type !== 'USER' && (
              <span className="flex-shrink-0 text-[11px] tabular-nums text-muted-foreground">{labels.memberCount(count)}</span>
            )
          )}
        </button>
      </div>
    </div>
  );

  const childrenGroup =
    isExpanded && hasChildren ? (
      <div role="group">
        {node.children.map((child) => (
          <OrgTree
            key={child.id}
            node={child}
            selectedNodeId={selectedNodeId}
            expandedNodes={expandedNodes}
            onSelect={onSelect}
            onToggle={onToggle}
            onExpand={onExpand}
            depth={depth + 1}
            labels={labels}
          />
        ))}
      </div>
    ) : null;

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
