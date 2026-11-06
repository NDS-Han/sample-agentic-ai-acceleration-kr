// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import Link from 'next/link';

import { getBodyLoggingAction } from '@/lib/actions/settings';
import { fetchMonitoringEvents } from '@/lib/actions/monitoring';
import { BodyLogsTabs } from '@/components/monitoring/BodyLogsTabs';

/**
 * 본문 로그 뷰어 — 요청/응답 본문을 request_id 로 조회한다.
 *
 * 운영 결정: 열람은 수집 토글과 연동한다 — 토글이 꺼져 있으면 이 페이지는
 * 과거 수집분도 보여주지 않는다(끄면 "프롬프트 접근 경로 전체"가 닫히는 게
 * 의도된 동작). 백엔드도 같은 플래그로 403 을 낸다.
 */
export default async function BodyLogsPage() {
  const t = await getTranslations('monitoring.bodyLogs');

  const result = await getBodyLoggingAction();
  const enabled = result.success ? result.data.enabled : null;

  if (enabled !== true) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">{t('pageTitle')}</h1>
        <div className="glass rounded-apple p-6">
          <p className="text-sm text-muted-foreground">
            {enabled === null ? t('statusUnknown') : t('disabledNotice')}
          </p>
          <Link href="/monitoring" className="mt-3 inline-block text-sm text-primary hover:underline">
            {t('backToMonitoring')}
          </Link>
        </div>
      </div>
    );
  }

  // 과거 요청도 찾아야 본문 조회가 유용하다 — 기간 제한 없이 최신 순 상한까지.
  // (usage_logs 가 인덱스, S3 본문 보존은 90일이라 그 이전은 자연히 404 안내가 뜬다)
  const events = await fetchMonitoringEvents(200, 'all', 0).catch(() => ({ events: [] }));
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t('pageTitle')}</h1>
        <Link href="/monitoring" className="text-sm text-primary hover:underline">
          {t('backToMonitoring')}
        </Link>
      </div>
      {/* 마스킹 없는 원문이고 열람이 감사된다는 건 화면 어디서든 보여야 한다 —
          어느 탭에 들어가든 상시 고지. */}
      <p className="text-xs text-muted-foreground">{t('auditNotice')}</p>
      <BodyLogsTabs events={events} />
    </div>
  );
}
