'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * /users 우측 상세 — 노드 타입별로 나눈다.
 *   ORGANIZATION: 규모 + 조직 앱 접근 정책(대화상자)
 *   DEPARTMENT  : 규모
 *   TEAM        : TeamDetail
 *   USER        : UserDetail
 * 정책 편집은 항목별 대화상자가 그 항목만 저장한다 — 예전의 아코디언 인라인 편집기와
 * 하단 'Unsaved changes' 바는 없앴다.
 */

import { useEffect, useState } from 'react';
import { useTranslations } from 'next-intl';

import { getScopeAllowedClientsAction } from '@/lib/actions/users';
import type { OrgTreeNode } from '@/types/entities';
import { findNodeById, findNodeExpandPath } from '@/lib/utils/orgSearch';
import { AppAccessDialog } from './detail/AppAccessDialog';
import { PolicyRow, Section } from './detail/parts';
import { TeamDetail } from './detail/TeamDetail';
import { UserDetail } from './detail/UserDetail';
import { usePolicyText } from './detail/usePolicyText';

interface OrgDetailPanelProps {
  node: OrgTreeNode | null;
  /** 트리 루트 — 상위 경로(빵부스러기)·소속 팀 찾기. */
  root?: OrgTreeNode | null;
  /** 팀 구성원 표의 행 → 사용자 선택. */
  onOpenUser?: (_id: string) => void;
  /** 저장 성공 후 트리 재조회 — 개별설정 점·팀장 표시가 stale 해지지 않게. */
  onSaved?: () => void;
}

/** 루트에서 node 까지 조상 노드들(자신 제외). */
function ancestorsOf(root: OrgTreeNode | null | undefined, id: string): OrgTreeNode[] {
  if (!root) return [];
  const path = findNodeExpandPath(root, id) ?? [];
  return path.map((pid) => findNodeById(root, pid)).filter((n): n is OrgTreeNode => !!n);
}

export function OrgDetailPanel({ node, root, onOpenUser, onSaved }: OrgDetailPanelProps) {
  const t = useTranslations('users');
  const tDetail = useTranslations('usersDetail');

  if (!node) {
    return (
      <div className="flex h-full min-h-[200px] items-center justify-center text-sm text-muted-foreground">
        {t('selectNodePrompt')}
      </div>
    );
  }

  const ancestors = ancestorsOf(root, node.id);
  const crumbs = ancestors.filter((a) => a.type === 'ORGANIZATION' || a.type === 'DEPARTMENT').map((a) => a.name);

  if (node.type === 'TEAM') {
    return (
      <TeamDetail
        key={node.id}
        node={node}
        breadcrumb={crumbs}
        onOpenUser={(id) => onOpenUser?.(id)}
        onChanged={() => onSaved?.()}
      />
    );
  }
  if (node.type === 'USER') {
    const team = [...ancestors].reverse().find((a) => a.type === 'TEAM') ?? null;
    return (
      <UserDetail
        key={node.id}
        node={node}
        teamId={team?.id ?? null}
        breadcrumb={team ? [...crumbs, team.name] : crumbs}
        onChanged={() => onSaved?.()}
      />
    );
  }
  if (node.type === 'ORGANIZATION') return <OrgSummary key={node.id} node={node} />;
  if (node.type === 'DEPARTMENT') {
    const td = tDetail;
    const teamCount = node.meta.team_count ?? node.children?.length ?? 0;
    return (
      <div className="space-y-4">
        {crumbs.length > 0 && <p className="text-xs text-muted-foreground">{crumbs.join(' › ')}</p>}
        <h2 className="text-xl font-bold">{node.name}</h2>
        <p className="text-sm text-muted-foreground">
          {/* 팀 수 자리에는 팀 수(team_count, 없으면 children 수) — 사람 수로 폴백하지 않는다.
              사람 수를 모르면(null) 그 부분을 아예 그리지 않는다. */}
          {node.meta.member_count == null
            ? td('org.deptTeamsOnly', { teams: teamCount })
            : td('org.deptSummary', { teams: teamCount, people: node.meta.member_count })}
        </p>
      </div>
    );
  }
  return null;
}

function OrgSummary({ node }: { node: OrgTreeNode }) {
  const td = useTranslations('usersDetail');
  const text = usePolicyText([]);
  const [clients, setClients] = useState<string[] | null | 'failed'>(null);
  const [open, setOpen] = useState(false);
  const [gen, setGen] = useState(0);

  useEffect(() => {
    let alive = true;
    getScopeAllowedClientsAction('organization', node.id).then((r) => {
      if (alive) setClients(r.success ? r.data.clients : 'failed');
    });
    return () => {
      alive = false;
    };
  }, [node.id, gen]);

  const own = Array.isArray(clients) && clients.length > 0;
  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-xl font-bold">{node.name}</h2>
        <p className="mt-1 text-sm text-muted-foreground">
          {td('org.summary', {
            depts: node.children?.length ?? 0,
            teams: node.meta.team_count ?? 0,
            people: node.meta.member_count ?? 0,
          })}
        </p>
      </div>
      <Section title={td('policies.title')}>
        <PolicyRow
          testId="policy-apps"
          label={td('policies.apps')}
          value={
            clients === null
              ? '…'
              : clients === 'failed'
                ? td('policies.loadFailed')
                : text.apps({ allowed: own ? clients : null, source: own ? 'own' : 'none' })
          }
          sub={td('org.appsSub')}
          source={Array.isArray(clients) ? (own ? 'org' : 'none') : null}
          scope="team"
          action={{ kind: 'button', label: td('policies.change'), onClick: () => setOpen(true), disabled: clients === 'failed' }}
        />
      </Section>
      <AppAccessDialog
        isOpen={open}
        onClose={() => setOpen(false)}
        scope="organization"
        id={node.id}
        target={node.name}
        inheritedText=""
        onSaved={() => setGen((g) => g + 1)}
      />
    </div>
  );
}
