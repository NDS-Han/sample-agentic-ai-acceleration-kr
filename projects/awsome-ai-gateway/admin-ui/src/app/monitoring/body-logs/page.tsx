// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { getTranslations } from 'next-intl/server';
import Link from 'next/link';

import { getBodyLoggingAction } from '@/lib/actions/settings';
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
  // 읽기 실패(enabled=null)면 statusUnknown 잠금이 먼저 나가니 여기선 true 로 둔다.
  const provisioned = result.success ? result.data.provisioned !== false : true;

  // stale ON + 미프로비저닝이면 뷰어 API 도 버킷 미설정으로 503 — 화면 자체를
  // 뷰어가 아닌 안내 상태로 둔다(토글·EventLog 링크와 같은 잠금 방향).
  // enabled=false & 미프로비저닝(정상 OFF)은 일반 disabled 안내로 둔다 —
  // "플래그가 ON으로 남아 있다"는 안내를 꺼진 운영자에게 띄우면 안 된다.
  if (enabled !== true || !provisioned) {
    return (
      <div className="space-y-4">
        <h1 className="text-2xl font-bold">{t('pageTitle')}</h1>
        <div className="glass rounded-apple p-6">
          <p className="text-sm text-muted-foreground">
            {enabled === null
              ? t('statusUnknown')
              : enabled === true && !provisioned
                ? t('staleOnNotice')
                : !provisioned
                  ? t('notProvisionedNotice')
                  : t('disabledNotice')}
          </p>
          <Link href="/monitoring" className="mt-3 inline-block text-sm text-primary hover:underline">
            {t('backToMonitoring')}
          </Link>
        </div>
      </div>
    );
  }

  // 최근 이벤트 목록은 기본 탭(세션)이 쓰지 않는다 — 첫 페인트를 막지 않도록
  // 요청별 탭이 마운트될 때 클라이언트에서 가져온다(실패도 빈 목록이 아니라
  // 명시 실패 상태로 보인다).
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">{t('pageTitle')}</h1>
        <Link href="/monitoring" className="text-sm text-primary hover:underline">
          {t('backToMonitoring')}
        </Link>
      </div>
      {/* 마스킹 없는 원문이고 열람이 감사된다는 건 화면 어디서든 보여야 한다 —
          어느 탭에 들어가든 상시 고지. 고지는 이 한 곳에 두고(검색 힌트·버튼
          라벨의 중복 문구는 지운다) 눈에 띄는 콜아웃으로 표시한다. */}
      <div className="rounded-lg border border-amber-300/60 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-500/30 dark:bg-amber-500/10 dark:text-amber-300">
        {t('auditNotice')}
      </div>
      <BodyLogsTabs />
    </div>
  );
}
