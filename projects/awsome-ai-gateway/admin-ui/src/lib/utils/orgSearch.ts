// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 조직 트리의 클라이언트 사이드 검색 유틸.
 *
 * 부서/팀은 `/admin/users/tree` payload 에 이미 전부 들어있으므로 서버를 치지
 * 않고 즉시 매칭한다. 사용자(USER)는 트리에 lazy-load 되어 payload 에 없으므로
 * `/admin/users/search` 를 쓴다 — 이 모듈은 사용자를 다루지 않는다.
 */

import type { OrgTreeNode } from '@/types/entities';

/** 부서/팀 검색 결과 1건. */
export interface OrgMatch {
  node: OrgTreeNode;
  /** 루트~부모까지의 노드 id. 트리에서 이 노드를 보이게 하려면 전부 펼쳐야 한다. */
  ancestorIds: string[];
  /** 팀이면 소속 부서명, 부서면 null — 드롭다운 부제로 표시. */
  parentName: string | null;
}

export interface OrgSearchResult {
  matches: OrgMatch[];
  /** limit 에서 잘렸는지 — UI 가 "결과가 더 있습니다" 힌트를 띄운다. */
  truncated: boolean;
}

/** 서버 검색(admin-api SEARCH_MIN_LEN)과 동일해야 한다. */
export const SEARCH_MIN_LENGTH = 2;

/** ORGANIZATION 은 단일 루트라 검색 대상에서 제외 — 매칭돼도 이동할 곳이 없다. */
const SEARCHABLE_TYPES = new Set<OrgTreeNode['type']>(['DEPARTMENT', 'TEAM']);

/**
 * 부서·팀명 부분 일치 검색. 대소문자 무시.
 *
 * 정렬은 prefix 일치를 먼저 노출한다 — "SW" 를 치면 "SW개발팀" 이
 * "차세대SW팀" 보다 위에 온다. 동순위는 이름 로케일 정렬(안정적).
 */
export function searchOrgNodes(
  root: OrgTreeNode | null,
  term: string,
  limit = 20,
): OrgSearchResult {
  const needle = term.trim().toLowerCase();
  if (!root || needle.length < SEARCH_MIN_LENGTH) {
    return { matches: [], truncated: false };
  }

  const found: OrgMatch[] = [];
  const walk = (node: OrgTreeNode, ancestorIds: string[], parentName: string | null) => {
    if (SEARCHABLE_TYPES.has(node.type) && node.name.toLowerCase().includes(needle)) {
      found.push({ node, ancestorIds, parentName });
    }
    // 자식 순회는 매칭 여부와 무관 — 부모가 안 맞아도 자식은 맞을 수 있다.
    node.children?.forEach((child) =>
      walk(child, [...ancestorIds, node.id], node.name),
    );
  };
  walk(root, [], null);

  found.sort((a, b) => {
    const aPrefix = a.node.name.toLowerCase().startsWith(needle);
    const bPrefix = b.node.name.toLowerCase().startsWith(needle);
    if (aPrefix !== bPrefix) return aPrefix ? -1 : 1;
    return a.node.name.localeCompare(b.node.name);
  });

  return { matches: found.slice(0, limit), truncated: found.length > limit };
}

/**
 * 팀 id 로 트리 내 위치를 찾아 "펼쳐야 하는 노드 id 목록" 을 반환.
 *
 * 검색된 사용자를 트리에 드러내려면 조상(조직→부서)뿐 아니라 팀 자신도 펼쳐야
 * 한다(멤버가 lazy-load 되므로). 따라서 반환값에 teamId 가 포함된다.
 * 팀을 찾지 못하면 null — 비활성 팀이거나 멤버 0명이어서 트리에서 숨겨진 경우.
 */
export function findTeamExpandPath(
  root: OrgTreeNode | null,
  teamId: string,
): string[] | null {
  if (!root) return null;

  const walk = (node: OrgTreeNode, ancestorIds: string[]): string[] | null => {
    if (node.type === 'TEAM' && node.id === teamId) {
      return [...ancestorIds, node.id];
    }
    for (const child of node.children ?? []) {
      const hit = walk(child, [...ancestorIds, node.id]);
      if (hit) return hit;
    }
    return null;
  };

  return walk(root, []);
}
