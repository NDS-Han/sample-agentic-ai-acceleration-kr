// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { AppPolicyPanel } from '@/components/apps/AppPolicyPanel';

export default async function AppsPage() {
  const t = await getTranslations('apps');

  return (
    <div className="space-y-8">
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">{t('title')}</h1>
      </div>
      <AppPolicyPanel />
    </div>
  );
}
