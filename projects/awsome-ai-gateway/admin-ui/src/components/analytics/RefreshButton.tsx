'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useRouter } from 'next/navigation';
import { useTranslations } from 'next-intl';
import { RefreshCw } from 'lucide-react';

/** disabled — custom 구간이 유효하지 않아 새로 고칠 조회가 없을 때. */
export function RefreshButton({ disabled = false }: { disabled?: boolean }) {
  const router = useRouter();
  const t = useTranslations('analytics');

  return (
    <button
      type="button"
      onClick={() => router.refresh()}
      disabled={disabled}
      className={[
        'inline-flex items-center gap-1.5 rounded-md px-3 py-2 text-sm font-medium transition-colors',
        'border border-border bg-background hover:bg-muted',
        'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
        'disabled:pointer-events-none disabled:opacity-50',
      ].join(' ')}
      aria-label={t('refreshLabel')}
    >
      <RefreshCw size={14} aria-hidden="true" />
      {t('refresh')}
    </button>
  );
}