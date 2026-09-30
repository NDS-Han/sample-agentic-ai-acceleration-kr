// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { adminAPI } from '@/lib/api-client';
import type { RateLimitTreeNode } from '@/types/entities';
import { RateLimitTreeView } from '@/components/rate-limits/RateLimitTreeView';
import { ErrorState } from '@/components/common/ErrorState';

export default async function RateLimitsPage() {
  const t = await getTranslations('rateLimits');
  // 실패를 "설정 없음"과 구분 — 빈 트리로 접으면 한도 미설정처럼 보인다.
  const treeResult = await adminAPI
    .get<RateLimitTreeNode[]>('/admin/rate-limits/tree')
    .then((v) => ({ ok: true as const, value: v }))
    .catch(() => ({ ok: false as const }));

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">{t('title')}</h1>
      {treeResult.ok ? <RateLimitTreeView nodes={treeResult.value} /> : <ErrorState />}
    </div>
  );
}
