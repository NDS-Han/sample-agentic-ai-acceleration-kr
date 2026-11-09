// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { adminAPI } from '@/lib/api-client';
import type { ServiceAccountListResponse } from '@/types/entities';
import { ServiceAccountsPanel } from '@/components/service-accounts/ServiceAccountsPanel';

export const dynamic = 'force-dynamic';

export default async function ServiceAccountsPage() {
  const t = await getTranslations('serviceAccounts');

  const saResult = await adminAPI
    .get<ServiceAccountListResponse>('/admin/service-accounts')
    .then((v) => ({ ok: true as const, value: v }))
    .catch(() => ({ ok: false as const }));

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">{t('pageTitle')}</h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {t('pageDescription')}
        </p>
      </div>

      {saResult.ok ? (
        <ServiceAccountsPanel
          accounts={saResult.value.items}
          maxTtlDays={saResult.value.max_ttl_days}
          // 필드 부재(=구버전 API)는 발급이 켜져 있던 시절 — !== false 유지.
          // 신버전 API 는 항상 명시값을 주므로 fail-closed 해석은 백엔드가 담당.
          serviceKeysEnabled={saResult.value.service_keys_enabled !== false}
        />
      ) : (
        <div className="glass rounded-apple p-8 text-center text-sm text-muted-foreground">
          {t('loadFailed')}
        </div>
      )}
    </div>
  );
}
