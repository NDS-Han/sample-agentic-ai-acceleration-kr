'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useRouter, usePathname } from 'next/navigation';
import { useLocale, useTranslations } from 'next-intl';
import type { AnalyticsFilterForm } from '@/types/api';
import type { GroupByType } from '@/types/enums';
import { currentCalendarMonth, monthsAgo } from '@/lib/utils/period';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';

interface AnalyticsFilterProps {
  defaultValue: AnalyticsFilterForm;
  /** 데이터가 있는 월 목록 (최신순). 월 선택기 옵션. */
  periods: string[];
  /** 현재 적용 중인 월 (YYYY-MM). custom 모드면 무시. */
  currentMonth: string;
}

export function AnalyticsFilter({ defaultValue, periods, currentMonth }: AnalyticsFilterProps) {
  const t = useTranslations('analytics');
  const locale = useLocale();
  const router = useRouter();
  const pathname = usePathname();

  const GROUP_BY_OPTIONS: { value: GroupByType; label: string }[] = [
    { value: 'model', label: t('groupByOptions.model') },
    { value: 'team', label: t('groupByOptions.team') },
    { value: 'user', label: t('groupByOptions.user') },
  ];
  const isCustom = defaultValue.period === 'custom';
  const reportingTz = useReportingTz();
  const thisMonth = currentCalendarMonth(reportingTz);
  const lastMonth = monthsAgo(1, reportingTz);

  function buildSearchParams(update: Partial<AnalyticsFilterForm>): string {
    const merged: AnalyticsFilterForm = { ...defaultValue, ...update };
    const entries = Object.entries(merged).filter(([, v]) => v != null && v !== '') as [
      string,
      string,
    ][];
    return new URLSearchParams(entries).toString();
  }

  // 월 선택 — period 에 YYYY-MM 을 직접 실어 보냄. custom 날짜는 비움.
  function handleMonthChange(month: string) {
    if (!isCustom && month === currentMonth) return;
    router.push(
      `${pathname}?${buildSearchParams({ period: month as never, start_date: null, end_date: null })}`,
    );
  }

  // 직접 입력(custom) 토글
  function enableCustom() {
    if (isCustom) return;
    router.push(`${pathname}?${buildSearchParams({ period: 'custom' })}`);
  }

  function handleGroupByChange(group_by: GroupByType) {
    router.push(`${pathname}?${buildSearchParams({ group_by })}`);
  }

  function handleStartDateChange(start_date: string) {
    router.push(`${pathname}?${buildSearchParams({ start_date })}`);
  }

  function handleEndDateChange(end_date: string) {
    router.push(`${pathname}?${buildSearchParams({ end_date })}`);
  }

  // 드롭다운 = 이번/지난 달 제외한 과거 데이터 월.
  const dropdownMonths = periods.filter((p) => p !== thisMonth && p !== lastMonth);
  const dropdownActive = !isCustom && currentMonth !== thisMonth && currentMonth !== lastMonth;
  const monthLabel = (period: string) => {
    const [year, month] = period.split('-').map(Number);
    return new Intl.DateTimeFormat(locale, { year: 'numeric', month: 'long', timeZone: 'UTC' }).format(
      new Date(Date.UTC(year, month - 1, 1)),
    );
  };

  const btn = (active: boolean) =>
    [
      'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
      active
        ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
        : 'text-muted-foreground interactive',
    ].join(' ');

  return (
    // 헤더 라인에 인라인으로 들어가는 컨트롤 — 별도 패널·라벨 없이 버튼 그룹
    // 자체 형태가 "기간"임을 드러낸다(대시보드 헤더와 같은 자리).
    <div className="flex flex-wrap items-center gap-2">
      {/* 기간 선택 — [이번 달][지난 달][기간선택 ▾][직접 입력] */}
      <div className="glass inline-flex items-center gap-0.5 rounded-apple-md p-1" role="group" aria-label={t('period')}>
        <button
          type="button"
          onClick={() => handleMonthChange(thisMonth)}
          aria-pressed={!isCustom && currentMonth === thisMonth}
          className={btn(!isCustom && currentMonth === thisMonth)}
        >
          {t('thisMonth')}
        </button>
        <button
          type="button"
          onClick={() => handleMonthChange(lastMonth)}
          aria-pressed={!isCustom && currentMonth === lastMonth}
          className={btn(!isCustom && currentMonth === lastMonth)}
        >
          {t('lastMonth')}
        </button>
        {dropdownMonths.length > 0 && (
          <span className={`relative inline-flex ${dropdownActive ? 'text-primary' : 'text-muted-foreground'}`}>
            <select
              aria-label={t('selectPeriod')}
              value={dropdownActive ? currentMonth : ''}
              onChange={(e) => {
                if (e.target.value) handleMonthChange(e.target.value);
              }}
              className={[btn(dropdownActive), 'appearance-none pr-7 cursor-pointer', dropdownActive ? '' : 'bg-transparent'].join(' ')}
            >
              <option value="" disabled>
                {t('selectPeriod')}
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
          </span>
        )}
        <button
          type="button"
          onClick={enableCustom}
          aria-pressed={isCustom}
          className={btn(isCustom)}
        >
          {t('customInput')}
        </button>
      </div>

      {/* 커스텀 날짜 범위 — 직접 입력 모드에서만 나타난다 */}
      {isCustom && (
        <div className="flex items-center gap-2">
          <input
            id="start_date"
            type="date"
            aria-label={t('startDate')}
            defaultValue={defaultValue.start_date ?? ''}
            max={defaultValue.end_date ?? undefined}
            onChange={(e) => handleStartDateChange(e.target.value)}
            className="rounded-apple-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          />
          <span className="text-muted-foreground">~</span>
          <input
            id="end_date"
            type="date"
            aria-label={t('endDate')}
            defaultValue={defaultValue.end_date ?? ''}
            min={defaultValue.start_date ?? undefined}
            onChange={(e) => handleEndDateChange(e.target.value)}
            className="rounded-apple-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
      )}

      {/* 그룹 기준 */}
      <select
        id="group_by"
        aria-label={t('groupBy')}
        defaultValue={defaultValue.group_by}
        onChange={(e) => handleGroupByChange(e.target.value as GroupByType)}
        className="rounded-apple-md border border-input bg-background px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-ring"
      >
        {GROUP_BY_OPTIONS.map((opt) => (
          <option key={opt.value} value={opt.value}>
            {opt.label}
          </option>
        ))}
      </select>
    </div>
  );
}
