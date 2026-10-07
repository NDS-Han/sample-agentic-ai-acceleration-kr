// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import Link from 'next/link';
import { cookies } from 'next/headers';
import { getTranslations } from 'next-intl/server';
import { parseJWT, isSessionExpired, roleFromCookie, ADMIN_ROLE_COOKIE } from '@/lib/auth';
import { UserRole } from '@/types/enums';

export default async function ForbiddenPage() {
  const t = await getTranslations('forbidden');

  // 역할별 홈 — DEVELOPER 는 '/' 자체가 403 이라 대시보드 링크로 보내면 다시 이
  // 페이지로 돌아오는 무한루프가 된다. 개발자 홈은 /my.
  const token = cookies().get('admin_jwt')?.value;
  let home = '/';
  let isAdmin = false;
  if (token) {
    try {
      const session = parseJWT(token);
      if (!isSessionExpired(session)) {
        // IdP id_token 에는 role 클레임이 없다 — layout 과 같은 규칙으로
        // admin_role 쿠키(admin-api 판정)를 우선 병합한다.
        const role =
          roleFromCookie(cookies().get(ADMIN_ROLE_COOKIE)?.value) ?? session.role;
        if (role === UserRole.DEVELOPER) {
          home = '/my';
        }
        isAdmin = role === UserRole.ADMIN;
      }
    } catch {
      // 토큰 손상 → 기본 홈으로
    }
  }

  return (
    <div className="flex flex-col items-center justify-center min-h-[500px] gap-6 p-8">
      <div className="flex flex-col items-center gap-4 text-center">
        <div className="flex h-20 w-20 items-center justify-center rounded-full bg-warning/10">
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="40"
            height="40"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
            className="text-warning"
            aria-hidden="true"
          >
            <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
        </div>
        <div className="flex flex-col gap-2">
          <h1 className="text-3xl font-bold text-foreground">403</h1>
          <h2 className="text-xl font-semibold text-foreground">{t('title')}</h2>
          <p className="text-sm text-muted-foreground max-w-sm">
            {/* ADMIN 에게 "관리자에게 문의" 는 자기 자신을 가리킨다 — 역할 제한
                페이지(/my 등)로 들어온 경우라고 설명을 분기한다. */}
            {isAdmin ? t('descriptionAdmin') : t('description')}
          </p>
        </div>
      </div>
      <Link
        href={home}
        className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground shadow hover:bg-primary/90 transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <svg
          xmlns="http://www.w3.org/2000/svg"
          width="16"
          height="16"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
          aria-hidden="true"
        >
          <path d="m3 9 9-7 9 7v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z" />
          <polyline points="9 22 9 12 15 12 15 22" />
        </svg>
        {t('backHome')}
      </Link>
    </div>
  );
}
