'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { AlertTriangle, RefreshCw } from 'lucide-react';

/**
 * 데이터 로딩 실패 상태 — "빈 화면"과 "조회 실패"를 구분한다.
 *
 * 서버 컴포넌트가 fetch 를 .catch 로 접으면 5xx·네트워크 blip 도 "데이터 없음"과
 * 구분이 안 되는 빈 테이블이 된다(운영 도구에서 최악의 거짓말). 실패 시엔 이
 * 컴포넌트를 렌더해 명시적으로 알리고 router.refresh() 재시도를 제공한다.
 */
export function ErrorState({ compact = false }: { compact?: boolean }) {
  const t = useTranslations('common');
  const router = useRouter();

  if (compact) {
    return (
      <div className="flex items-center justify-center gap-2 p-4 text-sm text-muted-foreground">
        <AlertTriangle size={14} className="text-amber-500" aria-hidden="true" />
        <span>{t('loadFailed')}</span>
        <button
          type="button"
          onClick={() => router.refresh()}
          className="inline-flex items-center gap-1 text-primary hover:underline underline-offset-2"
        >
          <RefreshCw size={12} aria-hidden="true" />
          {t('retry')}
        </button>
      </div>
    );
  }

  return (
    <div className="glass rounded-apple p-8 flex flex-col items-center gap-3 text-center">
      <div className="flex h-12 w-12 items-center justify-center rounded-full bg-amber-500/10">
        <AlertTriangle size={22} className="text-amber-500" aria-hidden="true" />
      </div>
      <p className="text-sm font-medium text-foreground">{t('loadFailed')}</p>
      <p className="text-xs text-muted-foreground max-w-sm">{t('loadFailedDesc')}</p>
      <button
        type="button"
        onClick={() => router.refresh()}
        className="mt-1 inline-flex items-center gap-1.5 rounded-md border border-border bg-background px-3 py-2 text-sm font-medium hover:bg-muted transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <RefreshCw size={14} aria-hidden="true" />
        {t('retry')}
      </button>
    </div>
  );
}
