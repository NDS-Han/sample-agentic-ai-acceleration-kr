// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 조직 트리 축약형(slim) 채우기 + 저장 후 팀 marks 패치.
 *  - slim 을 채우면 예전 전체형과 같은 모양(모든 meta, USER children=[], USER 팀 이름)
 *  - 채운 트리를 다시 채워도 같다(멱등)
 *  - marks 는 그 팀 노드만 바꾸고 나머지 노드는 같은 객체로 공유한다
 *  - 멤버 집합이 다르거나 팀이 없으면 null(전체 재조회 폴백)
 */

import { describe, expect, it } from 'vitest';

import type { OrgTreeNode } from '@/types/entities';
import { applyTeamMarks, hydrateOrgTree, type OrgTreeMarks } from '@/lib/utils/orgTreeHydrate';

const slim = {
  id: 'org',
  name: 'Org',
  type: 'ORGANIZATION',
  meta: { member_count: 2, team_count: 2 },
  children: [
    {
      id: 'd1',
      name: 'Dept',
      type: 'DEPARTMENT',
      meta: { member_count: 2, team_count: 2 },
      children: [
        {
          id: 't1',
          name: 'Alpha',
          type: 'TEAM',
          meta: { member_count: 2, custom_policy_count: 0, team_display_name: 'Dept_Alpha' },
          children: [
            { id: 'u1', name: 'Kim', type: 'USER', meta: { email: 'kim@x', role: 'DEVELOPER' } },
            { id: 'u2', name: 'Lee', type: 'USER', meta: { email: 'lee@x', role: 'DEVELOPER', has_custom_policies: true } },
          ],
        },
        { id: 't2', name: 'Empty', type: 'TEAM', meta: { member_count: 0, custom_policy_count: 0, team_display_name: 'Dept_Empty' }, children: [] },
      ],
    },
  ],
} as unknown as OrgTreeNode;

describe('hydrateOrgTree', () => {
  it('fills the full wire shape from the slim payload', () => {
    const root = hydrateOrgTree(slim)!;
    const team = root.children[0].children[0];
    const [kim, lee] = team.children;
    expect(kim.children).toEqual([]);
    expect(kim.meta).toEqual({
      member_count: null, team_count: null, leader_name: null, leader_user_id: null,
      email: 'kim@x', role: 'DEVELOPER', team_name: 'Alpha', has_custom_policies: false,
      custom_policy_count: null, team_display_name: 'Dept_Alpha',
    });
    expect(lee.meta.has_custom_policies).toBe(true);
    expect(team.meta.leader_user_id).toBeNull();
    expect(root.children[0].children[1].children).toEqual([]);
  });

  it('is idempotent and keeps values a full payload already had', () => {
    const once = hydrateOrgTree(slim);
    expect(hydrateOrgTree(once)).toEqual(once);
    const full = hydrateOrgTree(slim)!;
    full.children[0].children[0].children[0].meta.team_name = 'Server value';
    expect(hydrateOrgTree(full)!.children[0].children[0].children[0].meta.team_name).toBe('Server value');
  });

  it('returns null for an empty org', () => {
    expect(hydrateOrgTree(null)).toBeNull();
  });
});

describe('applyTeamMarks', () => {
  const marks: OrgTreeMarks = {
    team_id: 't1',
    leader_user_id: 'u1',
    leader_name: 'Kim',
    leader_email: 'kim@x',
    custom_policy_count: 1,
    members: [
      { id: 'u1', role: 'TEAM_LEADER', has_custom_policies: true },
      { id: 'u2', role: 'DEVELOPER', has_custom_policies: false },
    ],
  };

  it('patches only the team node and shares every other node', () => {
    const root = hydrateOrgTree(slim)!;
    const next = applyTeamMarks(root, marks)!;
    const team = next.children[0].children[0];
    expect(team.meta.leader_user_id).toBe('u1');
    expect(team.meta.leader_name).toBe('Kim');
    expect(team.meta.email).toBe('kim@x');
    expect(team.meta.custom_policy_count).toBe(1);
    expect(team.children.map((c) => [c.meta.role, c.meta.has_custom_policies])).toEqual([
      ['TEAM_LEADER', true],
      ['DEVELOPER', false],
    ]);
    // 다른 팀 노드는 그대로(같은 객체) — 패치가 트리 전체를 다시 만들지 않는다.
    expect(next.children[0].children[1]).toBe(root.children[0].children[1]);
    // 원본은 바뀌지 않는다.
    expect(root.children[0].children[0].children[0].meta.role).toBe('DEVELOPER');
  });

  it('returns null when membership changed or the team is missing (full reload)', () => {
    const root = hydrateOrgTree(slim)!;
    expect(applyTeamMarks(root, { ...marks, members: marks.members.slice(0, 1) })).toBeNull();
    expect(applyTeamMarks(root, { ...marks, team_id: 'nope' })).toBeNull();
    expect(applyTeamMarks(null, marks)).toBeNull();
  });
});
