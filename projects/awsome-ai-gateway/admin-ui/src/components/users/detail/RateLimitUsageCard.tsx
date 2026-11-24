'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 'Rate limit 사용량' 카드 — 팀·사용자 상세의 '적용 중인 정책' 바로 아래.
 * 기존 구성요소를 그대로 쓴다: LiveRpmCard(최근 60초 요청 vs RPM 한도) +
 * UsageTrendChart(1h/6h/24h/7d · RPM/TPM/CPH · 브라우저 TZ · 적용 한도 기준선).
 */

import { useTranslations } from 'next-intl';

import { LiveRpmCard } from '@/components/rate-limits/LiveRpmCard';
import { UsageTrendChart } from '@/components/rate-limits/UsageTrendChart';
import { Section } from './parts';
import type { RateSummary } from './policyRows';

export function RateLimitUsageCard({ scope, id, rate }: { scope: 'team' | 'user'; id: string; rate: RateSummary | null }) {
  const t = useTranslations('usersDetail.rateUsage');
  const eff = rate?.effective;
  const apiScope = scope === 'user' ? 'USER' : 'TEAM';
  return (
    <Section title={t('title')} aside={t('aside')} testId="rate-usage">
      <div className="space-y-3">
        <LiveRpmCard
          scope={apiScope}
          scopeId={id}
          rpmLimit={eff?.rpm ?? null}
          inherited={scope === 'user' && !rate?.own && !!rate?.team}
        />
        <UsageTrendChart
          scope={apiScope}
          scopeId={id}
          className=""
          limits={{ rpm: eff?.rpm ?? null, tpm: eff?.tpm ?? null, cph: eff?.cph ?? null }}
        />
      </div>
    </Section>
  );
}
