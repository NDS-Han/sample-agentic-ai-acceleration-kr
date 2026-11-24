// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { monthRange } from '@/lib/utils/trendSeries';

export interface DailyQueryPoint {
  date: string;
  /** 'MM-DD' 축 라벨. */
  label: string;
  /** 검색 수. 오늘 이후 날짜는 null(그리지 않음), 지난 빈 날은 0(평평한 막대). */
  queries: number | null;
}

const DAY_MS = 86_400_000;

/**
 * 일별 웹 검색 수 막대 데이터 — 비용 추이(CostTrendCard)와 같은 날짜 규칙:
 * 축은 기간(그 달 1일~말일) 전체, 리포팅 TZ 기준 오늘 이후는 null, 지난 빈 날은 0.
 * 달력 날짜로 센다(UTC 자정 + 24h — DST 전환일에도 날짜가 겹치거나 빠지지 않는다).
 */
export function buildDailyQuerySeries(
  period: string,
  daily: { date: string; queries: number }[],
  today: string,
): DailyQueryPoint[] {
  const range = monthRange(period);
  if (!range) return daily.map((d) => ({ date: d.date, label: d.date.slice(5), queries: d.queries }));
  const byDate = new Map(daily.map((d) => [d.date, d.queries]));
  const start = Date.parse(`${range.start}T00:00:00Z`);
  const end = Date.parse(`${range.end}T00:00:00Z`);
  const out: DailyQueryPoint[] = [];
  for (let t = start; t <= end; t += DAY_MS) {
    const date = new Date(t).toISOString().slice(0, 10);
    out.push({
      date,
      label: date.slice(5),
      queries: date > today ? null : (byDate.get(date) ?? 0),
    });
  }
  return out;
}
