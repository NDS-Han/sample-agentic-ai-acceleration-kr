'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';

interface Props {
  /** 카탈로그의 액션 그룹 키 목록. */
  groups: string[];
  currentGroup: string;
}

/**
 * 액션 분류 <select> — 기간 필터(필수 상대구간)와 나란히 둘 때 '전체' pill 이
 * '전체 기간'으로 오독되던 문제를 피하려고 분류는 select 로 분리한다.
 * 그룹 변경 시 세부 action 쿼리는 지운다(다른 그룹 액션이 남지 않게).
 */
export function GroupSelect({ groups, currentGroup }: Props) {
  const t = useTranslations('audit');
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  function onChange(value: string) {
    const sp = new URLSearchParams(searchParams.toString());
    if (value) sp.set('group', value);
    else sp.delete('group');
    sp.delete('action');
    router.push(`${pathname}?${sp.toString()}`);
  }

  return (
    <select
      value={currentGroup}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md border border-border bg-background px-3 py-1.5 text-sm"
      aria-label={t('groupFilterLabel')}
    >
      <option value="">{t('groupAll')}</option>
      {groups.map((g) => (
        <option key={g} value={g}>
          {t(`groups.${g}` as const)}
        </option>
      ))}
    </select>
  );
}
