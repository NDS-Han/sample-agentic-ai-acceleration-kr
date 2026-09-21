// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { adminAPI } from '@/lib/api-client';
import { AppPolicyPanel } from '@/components/apps/AppPolicyPanel';
import { WebSearchTogglePanel } from '@/components/apps/WebSearchTogglePanel';
import type { RoutingProfileItem } from '@/lib/actions/routing';

export default async function AppsPage() {
  const t = await getTranslations('apps');
  const tm = await getTranslations('models.webSearch');

  const routingRes = await adminAPI
    .get<{ items: RoutingProfileItem[] }>('/admin/routing-profiles')
    .catch(() => null);
  const routingProfiles = routingRes?.items ?? [];

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">{t('title')}</h1>
      </div>
      <AppPolicyPanel />

      <div>
        <h2 className="text-lg font-semibold mb-4">{tm('title')}</h2>
        <WebSearchTogglePanel initial={routingProfiles} />
      </div>
    </div>
  );
}
