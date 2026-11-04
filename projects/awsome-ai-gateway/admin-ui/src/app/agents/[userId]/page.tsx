// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { notFound } from 'next/navigation';
import { adminAPI } from '@/lib/api-client';
import { APIError } from '@/lib/utils/retry';
import { currentCalendarMonth } from '@/lib/utils/period';
import { ErrorState } from '@/components/common/ErrorState';
import type { ServiceAccountUsageResponse } from '@/types/entities';
import { ServiceAccountDetail } from '@/components/service-accounts/ServiceAccountDetail';

export const dynamic = 'force-dynamic';

interface Props {
  params: Promise<{ userId: string }>;
  searchParams: Promise<{ period?: string }>;
}

// 'YYYY-MM' + 달력상 유효한 월(01~12) — 잘못된 쿼리는 백엔드 400 대신 현재 월로 폴백.
const isValidPeriod = (p: string | undefined): p is string => {
  if (!p || !/^\d{4}-\d{2}$/.test(p)) return false;
  const month = Number(p.slice(5));
  return month >= 1 && month <= 12;
};

export default async function ServiceAccountDetailPage({
  params,
  searchParams,
}: Props) {
  const { userId } = await params;
  const { period: rawPeriod } = await searchParams;
  const period = isValidPeriod(rawPeriod)
    ? rawPeriod
    : currentCalendarMonth();

  const result = await adminAPI
    .get<ServiceAccountUsageResponse>(
      `/admin/service-accounts/${encodeURIComponent(userId)}/usage`,
      { period },
    )
    .then((v) => ({ ok: true as const, value: v }))
    .catch((e: unknown) => ({ ok: false as const, error: e }));

  if (!result.ok) {
    // 404 만 진짜 "없는 계정" — 401/500/네트워크 오류는 삼키지 않는다.
    if (result.error instanceof APIError && result.error.status === 404) {
      notFound();
    }
    return <ErrorState />;
  }
  const data = result.value;

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
    </div>
  );
}
