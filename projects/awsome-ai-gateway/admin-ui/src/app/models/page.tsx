// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { adminAPI } from '@/lib/api-client';
import { mapToModelListItem, type APIModelItem } from '@/lib/utils/modelMapping';
import { ModelsTable } from '@/components/models/ModelsTable';
import { CreateModelButton } from '@/components/models/CreateModelButton';
import { PriceSyncButton } from '@/components/models/PriceSyncButton';
import { ErrorState } from '@/components/common/ErrorState';

// 모델 카탈로그 관리 전용 페이지. 팀/유저별 모델 접근 정책은 /users 조직 트리의
// 팀 패널이 소유하고, 앱별 웹서치 토글은 /apps AppPolicyPanel이 소유한다.
export default async function ModelsPage() {
  const t = await getTranslations('models');

  const modelsRes = await adminAPI
    .get<{ items: APIModelItem[] }>('/admin/models')
    .then((v) => ({ ok: true as const, value: v }))
    .catch(() => ({ ok: false as const }));

  const models = (modelsRes.ok && modelsRes.value?.items ? modelsRes.value.items : []).map(
    mapToModelListItem,
  );

  return (
    <div className="space-y-8">
      <div>
        <div className="flex items-center justify-between mb-6">
          <h1 className="text-2xl font-bold">{t('title')}</h1>
          <div className="flex items-center gap-2">
            <PriceSyncButton />
            <CreateModelButton />
          </div>
        </div>
        {modelsRes.ok ? (
          <ModelsTable models={models} />
        ) : (
          // 조회 실패를 "모델 0개"와 구분 — 재시도 가능한 실패 상태로 표시.
          <ErrorState />
        )}
      </div>
    </div>
  );
}
