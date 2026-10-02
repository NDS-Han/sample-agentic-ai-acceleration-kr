'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname, useSearchParams } from 'next/navigation';
import { useLocale, useTranslations } from 'next-intl';
import {
  currentCalendarMonth,
  monthsAgo,
} from '@/lib/utils/period';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';

interface PeriodSelectorProps {
  periods: string[]; // 선택 가능한 월 (YYYY-MM), 최신순
  current: string;
}

/**
 * 기간 선택기 — 월이 계속 쌓여도 안 깨지는 상대 구조.
 *   [이번 달] [지난 달] [기간 선택 ▾]
 * 이번 달/지난 달은 항상 고정 버튼, 그 외 월은 드롭다운에서 선택.
 * 선택은 ?period=YYYY-MM URL 쿼리로 구동되어 서버 컴포넌트가 리렌더된다.
 */
export function PeriodSelector({ periods, current }: PeriodSelectorProps) {
  const t = useTranslations('dashboard');
  const locale = useLocale();
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const reportingTz = useReportingTz();

  const monthLabel = (period: string) => {
    const [year, month] = period.split('-').map(Number);
    return new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long', timeZone: 'UTC' }).format(
      new Date(Date.UTC(year, month - 1, 1)),
    );
  };

  const thisMonth = currentCalendarMonth(reportingTz);
  const lastMonth = monthsAgo(1, reportingTz);

  function go(period: string) {
    if (period === current) return;
    // 기존 쿼리(예: ?client=)를 보존한 채 period 만 교체 — 월 변경 시 client 필터가
    // 풀리지 않도록.
    const sp = new URLSearchParams(searchParams.toString());
    sp.set("period", period);
    router.push(`${pathname}?${sp.toString()}`);
  }

  if (periods.length === 0) {
    return <span className="text-sm text-muted-foreground">{t('noPeriods')}</span>;
  }

  // 드롭다운 옵션 = 이번 달/지난 달을 제외한 나머지 월(보통 과거 데이터 월).
  // 이번/지난 달에 데이터가 없어도 버튼은 항상 보이므로 여기선 제외.
  const dropdownMonths = periods.filter((p) => p !== thisMonth && p !== lastMonth);

  // current 가 이번/지난 달 중 어느 것도 아니면 드롭다운이 활성(특정 과거 월 선택 상태).
  const dropdownActive = current !== thisMonth && current !== lastMonth;

  const btn = (active: boolean) =>
    [
      'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
      active
        ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
        : 'text-muted-foreground interactive',
    ].join(' ');

  return (
    <div
      role="group"
      aria-label={t('periodSelect')}
      className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1"
    >
      <button
        type="button"
        onClick={() => go(thisMonth)}
        aria-pressed={current === thisMonth}
        className={btn(current === thisMonth)}
      >
        {t('thisMonth')}
      </button>
      <button
        type="button"
        onClick={() => go(lastMonth)}
        aria-pressed={current === lastMonth}
        className={btn(current === lastMonth)}
      >
        {t('lastMonth')}
      </button>

      {dropdownMonths.length > 0 && (
        <div className={`relative ${dropdownActive ? 'text-primary' : 'text-muted-foreground'}`}>
          <select
            aria-label={t('periodSelectMonth')}
            value={dropdownActive ? current : ''}
            onChange={(e) => {
              if (e.target.value) go(e.target.value);
            }}
            className={[btn(dropdownActive), 'appearance-none pr-7 cursor-pointer', dropdownActive ? '' : 'bg-transparent'].join(' ')}
          >
            <option value="" disabled>
              {t('periodSelect')}
            </option>
            {dropdownMonths.map((p) => (
              <option key={p} value={p}>
                {monthLabel(p)}
              </option>
            ))}
          </select>
          <svg
            aria-hidden="true"
            className="pointer-events-none absolute right-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </div>
      )}
    </div>
  );
}
