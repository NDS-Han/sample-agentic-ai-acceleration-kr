// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { adminAPI } from '@/lib/api-client';
import { getTranslations } from 'next-intl/server';
import type { OrgTreeNode } from '@/types/entities';
import { OrgTreeView } from '@/components/users/OrgTreeView';
import { CognitoSyncButton } from '@/components/users/CognitoSyncButton';
import { ErrorState } from '@/components/common/ErrorState';
import { RegisterScreenContext } from '@/components/chat/RegisterScreenContext';

/** 트리를 순회해 노드 타입별 개수만 집계. email/이름 등 PII 는 일절 미수집. */
function countOrgNodes(node: OrgTreeNode | null): Record<string, number> {
  const counts: Record<string, number> = {};
  const walk = (n: OrgTreeNode) => {
    counts[n.type] = (counts[n.type] ?? 0) + 1;
    n.children?.forEach(walk);
  };
  if (node) walk(node);
  return counts;
}

export default async function UsersPage() {
  const t = await getTranslations('users');
  // null = 조직 데이터 없음 / failed = 조회 실패 — 빈 트리와 실패를 구분한다.
  const orgTreeResult = await adminAPI
    .get<OrgTreeNode>('/admin/users/tree')
    .then((v) => ({ ok: true as const, value: v }))
    .catch(() => ({ ok: false as const }));
  const orgTree = orgTreeResult.ok ? orgTreeResult.value : null;

  // 퀵챗 화면 컨텍스트용 — 조직 구조의 "규모(개수)"만. 사용자 이메일/이름/리더명은
  // 절대 동봉하지 않는다(PII). 상세는 agent 가 query_db 로 직접 조회.
  const counts = countOrgNodes(orgTree);

  return (
    <div>
      {/* 퀵챗 화면 컨텍스트 등록(렌더 null) — 조직 규모 개수만. PII 없음. */}
      <RegisterScreenContext
        page="사용자/팀 관리"
        data={{
          부서수: counts.DEPARTMENT ?? 0,
          팀수: counts.TEAM ?? 0,
          사용자수: counts.USER ?? 0,
        }}
      />

      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">{t('title')}</h1>
        <div className="flex items-center gap-3">
          <CognitoSyncButton />
          <p className="text-xs text-muted-foreground">
            {t('cognitoNote')}
          </p>
        </div>
      </div>
      {orgTreeResult.ok ? <OrgTreeView root={orgTree} /> : <ErrorState />}
    </div>
  );
}
