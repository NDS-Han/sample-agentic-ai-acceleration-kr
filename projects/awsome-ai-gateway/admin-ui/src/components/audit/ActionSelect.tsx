'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname, useSearchParams } from 'next/navigation';
import { useTranslations } from 'next-intl';

interface Props {
  /** 현재 그룹의 액션 목록 — 비면 select 자체를 렌더하지 않는다. */
  actions: string[];
  currentAction: string;
  /** 라벨 해소용 — 카탈로그에 있는 액션만 i18n 키를 조회한다. */
  knownActions: string[];
}

/**
 * 세부 액션 <select> — 선택 시 다른 쿼리를 보존한 채 action 만 교체
 * (PeriodSelector.go 패턴). 값이 없는 option 은 action 쿼리를 제거한다.
 */
export function ActionSelect({ actions, currentAction, knownActions }: Props) {
  const t = useTranslations('audit');
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();

  if (actions.length === 0) return null;
  const known = new Set(knownActions);

  function onChange(value: string) {
    const sp = new URLSearchParams(searchParams.toString());
    if (value) sp.set('action', value);
    else sp.delete('action');
    router.push(`${pathname}?${sp.toString()}`);
  }

  return (
    <select
      value={currentAction}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-md border border-border bg-background px-3 py-1.5 text-sm"
      aria-label={t('actionFilterLabel')}
    >
      <option value="">{t('actionAllInGroup')}</option>
      {actions.map((a) => (
        <option key={a} value={a}>
          {known.has(a) ? t(`actions.${a}` as const) : a}
        </option>
      ))}
    </select>
  );
}
