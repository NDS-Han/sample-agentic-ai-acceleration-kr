// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import Link from 'next/link';
import { getTranslations } from 'next-intl/server';

export default async function NotFound() {
  const t = await getTranslations('errors');
  const tf = await getTranslations('forbidden');
  return (
    <div className="flex flex-col items-center justify-center min-h-[400px] gap-6 p-8">
      <div className="flex flex-col items-center gap-3 text-center">
        <div className="flex h-16 w-16 items-center justify-center rounded-full bg-muted">
          <span className="text-2xl font-bold text-muted-foreground" aria-hidden="true">
            404
          </span>
        </div>
        <h1 className="text-xl font-semibold">{t('notFound')}</h1>
        <p className="text-sm text-muted-foreground">{t('generic')}</p>
      </div>
      <Link
        href="/"
        className="inline-flex items-center rounded-md border border-border bg-background px-4 py-2 text-sm font-medium hover:bg-accent transition-colors"
      >
        {tf('backHome')}
      </Link>
    </div>
  );
}
