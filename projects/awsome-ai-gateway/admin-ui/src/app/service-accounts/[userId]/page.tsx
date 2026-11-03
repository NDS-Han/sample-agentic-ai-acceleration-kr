// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import { notFound } from 'next/navigation';
import { adminAPI } from '@/lib/api-client';
import { currentCalendarMonth } from '@/lib/utils/period';
import type { ServiceAccountUsageResponse } from '@/types/entities';
import { ServiceAccountDetail } from '@/components/service-accounts/ServiceAccountDetail';

export const dynamic = 'force-dynamic';

interface Props {
  params: Promise<{ userId: string }>;
  searchParams: Promise<{ period?: string }>;
}

export default async function ServiceAccountDetailPage({
  params,
  searchParams,
}: Props) {
  const { userId } = await params;
  const { period: rawPeriod } = await searchParams;
  const t = await getTranslations('serviceAccounts');
  const period = /^\d{4}-\d{2}$/.test(rawPeriod ?? '')
    ? (rawPeriod as string)
    : currentCalendarMonth();

  const data = await adminAPI
    .get<ServiceAccountUsageResponse>(
      `/admin/service-accounts/${userId}/usage`,
      { period },
    )
    .catch(() => null);

  if (!data) notFound();

  return (
    <div>
      <div className="mb-6">
        <h1 className="text-2xl font-bold">
          {data.display_name || data.email}
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          {data.email}
          {data.team_name ? ` · ${data.team_name}` : ''}
        </p>
      </div>
      <ServiceAccountDetail data={data} period={period} />
      <p className="mt-6 text-xs text-muted-foreground">
        {t('attributionNote', {
          since: new Date(data.attribution_since).toLocaleDateString(),
        })}
      </p>
    </div>
  );
}
