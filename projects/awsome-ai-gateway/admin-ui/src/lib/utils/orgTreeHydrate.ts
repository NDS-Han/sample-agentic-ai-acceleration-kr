// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 조직 트리 와이어 축약형(`/admin/users/tree?slim=true`) ↔ 화면 모델.
 *
 * 축약형은 None·false 점 표시·USER 의 빈 children·USER 의 팀 이름(부모 TEAM 노드에 있다)을
 * 뺀다 — 6k 사용자 조직에서 payload 가 1/3 이하로 준다. 화면 코드는 예전 모양(모든 meta 필드,
 * USER children=[])을 가정하므로 받은 직후 `hydrateOrgTree` 로 채운다. 이미 채워진 트리를 다시
 * 넣어도 같은 값이 나온다(멱등).
 *
 * `applyTeamMarks` — 저장 후 트리 전체(MB) 재조회 대신 `/admin/users/tree/marks?team_id=` 의
 * 작은 응답으로 그 팀 노드(팀장·멤버 역할·개별설정 점)만 고친다. 바뀐 경로의 노드만 새로
 * 만들고 나머지는 공유한다.
 */

import type { OrgNodeMeta, OrgTreeNode } from '@/types/entities';

const META_DEFAULTS: OrgNodeMeta = {
  member_count: null,
  team_count: null,
  leader_name: null,
  leader_user_id: null,
  email: null,
  role: null,
  team_name: null,
  has_custom_policies: null,
  custom_policy_count: null,
  team_display_name: null,
};

function hydrateNode(node: OrgTreeNode, team: OrgTreeNode | null): OrgTreeNode {
  const meta: OrgNodeMeta = { ...META_DEFAULTS, ...node.meta };
  if (node.type === 'USER') {
    if (team) {
      meta.team_name = meta.team_name ?? team.name;
      meta.team_display_name = meta.team_display_name ?? team.meta?.team_display_name ?? null;
    }
    meta.has_custom_policies = meta.has_custom_policies ?? false;
    return { ...node, meta, children: node.children ?? [] };
  }
  const parent = node.type === 'TEAM' ? node : team;
  return { ...node, meta, children: (node.children ?? []).map((c) => hydrateNode(c, parent)) };
}

export function hydrateOrgTree(root: OrgTreeNode | null | undefined): OrgTreeNode | null {
  return root ? hydrateNode(root, null) : null;
}

export interface OrgTreeMarks {
  team_id: string;
  leader_user_id: string | null;
  leader_name: string | null;
  leader_email: string | null;
  custom_policy_count: number;
  members: { id: string; role: OrgNodeMeta['role']; has_custom_policies: boolean }[];
}

/**
 * 팀 노드에 marks 를 적용한 새 트리. 팀이 트리에 없거나 활성 멤버 집합이 다르면(동기화로
 * 사람이 들고 났다) null — 호출부는 전체 재조회로 폴백한다.
 */
export function applyTeamMarks(root: OrgTreeNode | null, marks: OrgTreeMarks): OrgTreeNode | null {
  if (!root) return null;
  const byId = new Map(marks.members.map((m) => [m.id, m]));
  let ok = false;
  const walk = (node: OrgTreeNode): OrgTreeNode => {
    if (node.type === 'TEAM' && node.id === marks.team_id) {
      const kids = node.children ?? [];
      if (kids.length !== byId.size || kids.some((k) => !byId.has(k.id))) return node;
      ok = true;
      return {
        ...node,
        meta: {
          ...node.meta,
          leader_user_id: marks.leader_user_id,
          leader_name: marks.leader_name,
          email: marks.leader_email,
          custom_policy_count: marks.custom_policy_count,
        },
        children: kids.map((k) => {
          const m = byId.get(k.id)!;
          return { ...k, meta: { ...k.meta, role: m.role, has_custom_policies: m.has_custom_policies } };
        }),
      };
    }
    if (node.type === 'USER' || !node.children?.length) return node;
    let changed = false;
    const children = node.children.map((c) => {
      const n = walk(c);
      if (n !== c) changed = true;
      return n;
    });
    return changed ? { ...node, children } : node;
  };
  const next = walk(root);
  return ok ? next : null;
}
