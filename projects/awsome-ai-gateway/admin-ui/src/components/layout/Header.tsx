'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useLocale, useTranslations } from 'next-intl';
import { useRouter } from 'next/navigation';
import { useTransition } from 'react';
import type { AdminSession } from '@/types/entities';
import { LogOut, User, Globe } from 'lucide-react';
import { setLocale } from '@/lib/actions/locale';
import { RoleBadge } from '@/components/common/Badge';

interface HeaderProps {
  session: AdminSession | null;
}

export function Header({ session }: HeaderProps) {
  const t = useTranslations('auth');
  const locale = useLocale();
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  function toggleLocale() {
    const next = locale === 'ko' ? 'en' : 'ko';
    startTransition(async () => {
      await setLocale(next);
      router.refresh();
    });
  }

  return (
    <header className="flex h-16 flex-shrink-0 items-center justify-between border-b border-border bg-background px-6">
      {/* Left — page context (breadcrumb placeholder) */}
      <div className="flex items-center gap-2">
        <span className="text-sm text-muted-foreground">AWSome AI Gateway</span>
      </div>

      {/* Right — locale toggle + user info + logout */}
      <div className="flex items-center gap-4">
        <button
          onClick={toggleLocale}
          disabled={isPending}
          className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
          aria-label="Switch language"
        >
          <Globe size={15} aria-hidden="true" />
          <span>{locale === 'ko' ? 'EN' : 'KO'}</span>
        </button>
        {session ? (
          <>
            <div className="flex items-center gap-3">
              <div className="flex h-8 w-8 items-center justify-center rounded-full bg-muted">
                <User size={16} className="text-muted-foreground" aria-hidden="true" />
              </div>
              <div className="flex flex-col">
                <span className="text-sm font-medium text-foreground leading-none">
                  {session.display_name}
                </span>
                <span className="text-xs text-muted-foreground mt-0.5">{session.email}</span>
              </div>
              {session.role && (
                <RoleBadge
                  role={session.role}
                  label={t.has(`role.${session.role}`) ? t(`role.${session.role}`) : session.role}
                />
              )}
            </div>
            {/* Form POST (not fetch / not GET): browser submits POST and
                natively follows the 303 redirect with the new Set-Cookie,
                so the cookie is actually cleared. */}
            <form method="POST" action="/api/auth/logout" className="contents">
              <button
                type="submit"
                className="inline-flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                aria-label={t('logout')}
              >
                <LogOut size={15} aria-hidden="true" />
                <span>{t('logout')}</span>
              </button>
            </form>
          </>
        ) : (
          <span className="text-sm text-muted-foreground">{t('notAuthenticated')}</span>
        )}
      </div>
    </header>
  );
}