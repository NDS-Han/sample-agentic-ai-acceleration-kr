'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useLocale, useTranslations } from 'next-intl';
import { useRouter } from 'next/navigation';
import { useTransition } from 'react';
import type { AdminSession } from '@/types/entities';
import { LogOut, User, Globe } from 'lucide-react';
import { setLocale } from '@/lib/actions/locale';
import { RoleBadge } from '@/components/common/Badge';
import { EmailText } from '@/components/common/UnverifiedEmailBadge';

// 언어 이름·전환 안내는 자기 언어(endonym)로 고정한다 — 현재 언어를 못 읽는 사람도
// 버튼을 찾을 수 있어야 하므로 next-intl 메시지(현재 로케일)로 번역하지 않는다.
const LOCALE_UI: Record<string, { current: string; switchTo: string }> = {
  ko: { current: '한국어', switchTo: 'View in English' },
  en: { current: 'English', switchTo: '한국어로 보기' },
};

interface HeaderProps {
  session: AdminSession | null;
}

export function Header({ session }: HeaderProps) {
  const t = useTranslations('auth');
  const locale = useLocale();
  const router = useRouter();
  const [isPending, startTransition] = useTransition();
  const localeUi = LOCALE_UI[locale] ?? LOCALE_UI.en;

  function toggleLocale() {
    const next = locale === 'ko' ? 'en' : 'ko';
    startTransition(async () => {
      await setLocale(next);
      router.refresh();
    });
  }

  return (
    <header className="flex h-16 flex-shrink-0 items-center justify-end border-b border-border bg-background px-6">
      {/* locale toggle + user info + logout — 브랜드명은 사이드바에만 둔다(중복 제거) */}
      <div className="flex items-center gap-4">
        <button
          onClick={toggleLocale}
          disabled={isPending}
          title={localeUi.switchTo}
          className="inline-flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-sm font-medium text-muted-foreground hover:bg-accent hover:text-accent-foreground transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
          aria-label={`${localeUi.current} — ${localeUi.switchTo}`}
        >
          {/* 현재 언어를 표시한다 — 예전엔 전환될 언어(EN 화면에 'KO')를 보여 헷갈렸다. */}
          <Globe size={15} aria-hidden="true" />
          <span>{localeUi.current}</span>
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
                <span className="text-xs text-muted-foreground mt-0.5">
                  <EmailText email={session.email} />
                </span>
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