// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { Suspense } from 'react';
import { getTranslations } from 'next-intl/server';
import { SkeletonCard } from '@/components/common/SkeletonCard';
import {
  fetchMonitoringOverview,
  fetchMonitoringModels,
  fetchMonitoringEvents,
  fetchMonitoringUsers,
} from '@/lib/actions/monitoring';
import { getBodyLoggingAction } from '@/lib/actions/settings';
import { BodyLoggingToggle } from '@/components/monitoring/BodyLoggingToggle';
import { MonitoringOverview } from '@/components/monitoring/MonitoringOverview';
import { ModelHealthTable } from '@/components/monitoring/ModelHealthTable';
import { UserTopTable } from '@/components/monitoring/UserTopTable';
import { EventLog } from '@/components/monitoring/EventLog';
import { ErrorState } from '@/components/common/ErrorState';
import { RegisterScreenContext } from '@/components/chat/RegisterScreenContext';

// 각 섹션 fetch 가 throw 하면 Suspense 경계가 페이지 전체 error.tsx 로 새는 대신
// 그 섹션만 실패 카드로 저하시킨다 — 나머지 섹션은 정상 렌더.
async function OverviewSection() {
  const data = await fetchMonitoringOverview().catch(() => null);
  if (!data) return <ErrorState />;
  return (
    <>
      {/* 퀵챗 화면 컨텍스트 등록 — "지금 보는 모니터링 화면(최근 1시간 집계)".
          PII 없는 집계 수치만. 사용자가 "이 에러율 왜 높아?" 물으면 agent 가
          이 맥락 + query_db 로 답한다. */}
      <RegisterScreenContext
        page="실시간 모니터링"
        period="최근 1시간"
        data={{ last_1h: data.last_1h, active_models: data.active_models }}
      />
      <MonitoringOverview data={data} />
    </>
  );
}

async function ModelsSection() {
  const data = await fetchMonitoringModels().catch(() => null);
  return data ? <ModelHealthTable data={data} /> : <ErrorState />;
}

async function UsersSection() {
  const data = await fetchMonitoringUsers(10).catch(() => null);
  return data ? <UserTopTable data={data} /> : <ErrorState />;
}

async function EventsSection() {
  const data = await fetchMonitoringEvents().catch(() => null);
  return data ? <EventLog data={data} /> : <ErrorState />;
}

async function BodyLoggingSection() {
  const result = await getBodyLoggingAction();
  // 읽기 실패는 null(상태 불명)로 넘긴다 — OFF 로 렌더하면 실제로 수집 중인데
  // "꺼짐"으로 보여 프롬프트 캡처가 숨겨진다(프라이버시상 최악 방향).
  const enabled = result.success ? result.data.enabled : null;
  return <BodyLoggingToggle initialEnabled={enabled} />;
}

export default async function MonitoringPage() {
  const t = await getTranslations('monitoring');
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">{t('title')}</h1>

      {/* 본문 로깅 토글을 맨 위에 둔다 — 켜져 있으면 사용자 프롬프트가 durable
          저장소로 나가는 상태이므로, 이 화면을 열자마자 보여야 하는 정보다. */}
      <Suspense fallback={<SkeletonCard count={1} />}>
        <BodyLoggingSection />
      </Suspense>

      <Suspense fallback={<SkeletonCard count={6} />}>
        <OverviewSection />
      </Suspense>

      <Suspense fallback={<SkeletonCard count={1} />}>
        <ModelsSection />
      </Suspense>

      <Suspense fallback={<SkeletonCard count={1} />}>
        <UsersSection />
      </Suspense>

      <Suspense fallback={<SkeletonCard count={1} />}>
        <EventsSection />
      </Suspense>
    </div>
  );
}
