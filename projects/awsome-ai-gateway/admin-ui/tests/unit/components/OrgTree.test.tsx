// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 조직 트리 — 행 클릭은 선택(팀은 펼치기만, 접지 않음), 셰브런은 펼치기/접기만.
 * 사용자 행은 이름 + 이메일 두 줄(이름이 없으면 이메일만), 팀은 인원 수, 팀장 배지.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { useState } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { OrgTree, rowLabels } from '@/components/users/OrgTree';
import type { OrgTreeNode } from '@/types/entities';

const meta = (o: Partial<OrgTreeNode['meta']> = {}): OrgTreeNode['meta'] => ({
  member_count: null,
  team_count: null,
  leader_name: null,
  leader_user_id: null,
  email: null,
  role: null,
  team_name: null,
  ...o,
});
const user = (id: string, name: string, email: string, role: 'DEVELOPER' | 'TEAM_LEADER' = 'DEVELOPER'): OrgTreeNode => ({
  id,
  name,
  type: 'USER',
  children: [],
  meta: meta({ email, role }),
});
const ROOT: OrgTreeNode = {
  id: 'org',
  name: 'Default Organization',
  type: 'ORGANIZATION',
  meta: meta({ member_count: 2, team_count: 1 }),
  children: [
    {
      id: 'nds',
      name: 'NDS',
      type: 'DEPARTMENT',
      meta: meta({ member_count: 2, team_count: 1 }),
      children: [
        {
          id: 'dev',
          name: 'Developers',
          type: 'TEAM',
          meta: meta({ member_count: 2, team_display_name: 'NDS_Developers' }),
          children: [
            user('u1', 'seunghwan1228@gmail.com', 'seunghwan1228@gmail.com', 'TEAM_LEADER'),
            user('u2', 'Load Test', 'loadtest@mock.local'),
          ],
        },
      ],
    },
  ],
};

const LABELS = {
  emptyTeam: 'Empty',
  customPolicy: 'Own policy',
  leader: 'Leader',
  memberCount: (n: number) => `${n}`,
  expand: (n: string) => `Expand ${n}`,
  collapse: (n: string) => `Collapse ${n}`,
};

function Harness({ onSelect }: { onSelect: (_n: OrgTreeNode) => void }) {
  const [expanded, setExpanded] = useState(new Set(['org', 'nds']));
  const [sel, setSel] = useState<string | null>(null);
  return (
    <OrgTree
      node={ROOT}
      selectedNodeId={sel}
      expandedNodes={expanded}
      onSelect={(n) => {
        setSel(n.id);
        onSelect(n);
      }}
      onToggle={(id) =>
        setExpanded((p) => {
          const n = new Set(p);
          if (n.has(id)) n.delete(id);
          else n.add(id);
          return n;
        })
      }
      onExpand={(id) => setExpanded((p) => new Set([...p, id]))}
      labels={LABELS}
    />
  );
}

const item = (name: RegExp) => screen.getByRole('button', { name }).closest('[role="treeitem"]')!;

describe('OrgTree', () => {
  it('팀 행 클릭 = 선택 + 펼치기, 다시 클릭해도 접지 않는다', async () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    const u = userEvent.setup();
    const team = screen.getByRole('button', { name: /^Developers/ });
    await u.click(team);
    expect(onSelect).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'dev' }));
    expect(item(/^Developers/).getAttribute('aria-expanded')).toBe('true');
    expect(item(/^Developers/).getAttribute('aria-selected')).toBe('true');
    await u.click(team);
    expect(item(/^Developers/).getAttribute('aria-expanded')).toBe('true');
  });

  it('셰브런은 펼치기/접기만 — 선택은 바뀌지 않는다', async () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    const u = userEvent.setup();
    await u.click(screen.getByRole('button', { name: 'Expand Developers' }));
    expect(item(/^Developers/).getAttribute('aria-expanded')).toBe('true');
    await u.click(screen.getByRole('button', { name: 'Collapse Developers' }));
    expect(item(/^Developers/).getAttribute('aria-expanded')).toBe('false');
    expect(onSelect).not.toHaveBeenCalled();
    // 셰브런은 탭 순서에서 빠진다(행 버튼 + →/← 가 키보드 경로)
    expect((screen.getByRole('button', { name: 'Expand Developers' }) as HTMLButtonElement).tabIndex).toBe(-1);
  });

  it('부서 행 클릭은 선택만(펼침 상태 유지)', async () => {
    render(<Harness onSelect={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole('button', { name: /^NDS/ }));
    expect(item(/^NDS/).getAttribute('aria-expanded')).toBe('true');
    expect(item(/^NDS/).getAttribute('aria-selected')).toBe('true');
  });

  it('키보드 — Enter 는 선택(팀은 펼침), → / ← 로 펼치기/접기', () => {
    const onSelect = vi.fn();
    render(<Harness onSelect={onSelect} />);
    const team = screen.getByRole('button', { name: /^Developers/ });
    fireEvent.keyDown(team, { key: 'ArrowRight' });
    expect(item(/^Developers/).getAttribute('aria-expanded')).toBe('true');
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.keyDown(team, { key: 'ArrowLeft' });
    expect(item(/^Developers/).getAttribute('aria-expanded')).toBe('false');
    fireEvent.keyDown(team, { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ id: 'dev' }));
    expect(item(/^Developers/).getAttribute('aria-expanded')).toBe('true');
  });

  it('두 줄 라벨 — 팀은 짧은 이름 + 전체 이름·인원, 사용자는 이름 + 이메일(이름 없으면 이메일만)·팀장 배지', async () => {
    render(<Harness onSelect={vi.fn()} />);
    await userEvent.setup().click(screen.getByRole('button', { name: 'Expand Developers' }));
    const team = screen.getByRole('button', { name: /^Developers/ });
    expect(team.textContent).toBe('DevelopersNDS_Developers2');
    expect(team.getAttribute('title')).toBe('Developers · NDS_Developers');
    const lead = screen.getByRole('button', { name: /^seunghwan1228@gmail\.com/ });
    expect(lead.textContent).toBe('seunghwan1228@gmail.comLeader'); // 이메일만 한 줄 + 팀장
    const lt = screen.getByRole('button', { name: /^Load Test/ });
    expect(lt.textContent).toBe('Load Testloadtest@mock.local');
    expect(lt.getAttribute('title')).toBe('Load Test · loadtest@mock.local');
  });

  it('rowLabels — 표시 이름이 없는 구버전 팀 메타는 한 줄', () => {
    expect(rowLabels({ ...ROOT.children[0].children[0], meta: meta({ member_count: 1 }) })).toEqual({
      primary: 'Developers',
      secondary: null,
    });
  });
});
