// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

'use client';

/**
 * 비용 추이 카드 — 합계(굵은 선) + 팀별 추이(색·선형 구분) 멀티 시리즈.
 * 실데이터: /admin/analytics 의 trends[](합계) + trends_by_team[](팀별 일별).
 *
 * 팀이 많으면 기본 표시는 상위 5개 팀 + 나머지 '기타' 합산으로 접고,
 * 팀 칩 토글로 원하는 팀만 골라 볼 수 있게 한다 — 칩으로 켠 팀은 개별
 * 시리즈가 되고, 꺼진 팀은 '기타' 합산에 합쳐진다(합계와 어긋나지 않게).
 * recharts + chart 토큰(hsl(var(--chart-N)))으로 테마(다크/라이트) 자동 연동.
 * 데이터가 비면 빈 상태 표시(가짜 데이터 없음).
 */

import { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
  ResponsiveContainer,
  ComposedChart,
  Area,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from 'recharts';
import {
  buildTrendSeries,
  defaultSelectedTeamIds,
  type TeamSeriesMeta,
  type TeamTrendSeries,
  type TrendPoint,
} from '@/lib/utils/trendSeries';

export { defaultSelectedTeamIds };

interface CostTrendCardProps {
  trends: TrendPoint[];
  trendsByTeam?: TeamTrendSeries[];
}

export interface TeamSeriesDef {
  dataKey: string;
  teamId: string;
  name: string;
  color: string;
  dash: string;
  total: number;
}

export interface TrendChartData {
  data: Record<string, number | string | null>[];
  /** 현재 켜진 팀 시리즈(비용 내림차순) — 차트에 그릴 Line 들. */
  series: TeamSeriesDef[];
  /** 칩 렌더용 전체 팀 목록(비용 내림차순, 색/선형 부여 완료). */
  allTeams: TeamSeriesDef[];
  showOther: boolean;
  hasTeamSeries: boolean;
}

/**
 * 추이 차트 데이터 조립 — lib/utils/trendSeries 의 열 형태 결과를 recharts 가
 * 먹는 행(row) 형태로 바꾸는 얇은 어댑터. 조립 규칙(날짜 합집합·null·기타
 * 합산·상위 N 기본 선택)은 공용 유틸 문서를 본다.
 */
export function buildTrendChartData(
  trends: TrendPoint[],
  trendsByTeam: TeamTrendSeries[],
  selectedIds?: ReadonlySet<string>,
): TrendChartData {
  const { dates, total, series, allTeams, other, hasTeamSeries } =
    buildTrendSeries(trends, trendsByTeam, selectedIds);

  const data = dates.map((d, i) => {
    const row: Record<string, number | string | null> = {
      label: d.length >= 10 ? d.slice(5) : d,
      total: total[i],
    };
    for (const s of series) row[s.key] = s.values[i];
    if (other) row.other = other[i];
    return row;
  });

  const toDef = (m: TeamSeriesMeta): TeamSeriesDef => ({
    dataKey: m.key,
    teamId: m.teamId,
    name: m.name,
    color: m.color,
    dash: m.dash,
    total: m.total,
  });

  return {
    data,
    series: series.map(toDef),
    allTeams: allTeams.map(toDef),
    showOther: other !== null,
    hasTeamSeries,
  };
}

export function CostTrendCard({ trends, trendsByTeam = [] }: CostTrendCardProps) {
  const t = useTranslations('dashboard');
  const topIds = useMemo(() => defaultSelectedTeamIds(trendsByTeam), [trendsByTeam]);
  // null = 기본 선택(상위 N). 사용자가 칩을 누르면 명시 선택이 된다.
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const effective = picked ?? topIds;
  const { data, series, allTeams, showOther, hasTeamSeries } = useMemo(
    () => buildTrendChartData(trends, trendsByTeam, effective),
    [trends, trendsByTeam, effective],
  );

  const toggleTeam = (teamId: string) => {
    const next = new Set(effective);
    if (next.has(teamId)) next.delete(teamId);
    else next.add(teamId);
    setPicked(next);
  };

  return (
    // h-full + flex-col: 대시보드 그리드에서 옆 카드(모델 점유율) 높이까지 늘어날 때
    // 늘어난 높이를 차트가 채우도록 — 카드 하단에 빈 밴드가 생기지 않는다.
    <div className="glass glass-hover flex h-full flex-col rounded-apple p-5">
      <div className="text-sm font-semibold tracking-tight">{t('costTrendTitle')}</div>
      <div className="mb-3 text-xs text-muted-foreground">{t('costTrendSubtitle')}</div>

      {hasTeamSeries && data.length > 0 && (
        <div className="mb-3 flex flex-wrap gap-1.5">
          {allTeams.map((tm) => {
            const on = effective.has(tm.teamId);
            return (
              <button
                key={tm.teamId}
                type="button"
                aria-pressed={on}
                title={tm.name}
                onClick={() => toggleTeam(tm.teamId)}
                className={`inline-flex max-w-full items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                  on
                    ? 'border-transparent bg-secondary text-foreground'
                    : 'border-border text-muted-foreground hover:bg-secondary/60'
                }`}
              >
                <span
                  className="inline-block h-2.5 w-2.5 shrink-0 rounded-full"
                  style={{ backgroundColor: on ? tm.color : 'hsl(var(--muted-foreground))' }}
                />
                <span className="max-w-[12rem] truncate">{tm.name}</span>
              </button>
            );
          })}
        </div>
      )}

      {data.length === 0 ? (
        <div className="flex h-[180px] items-center justify-center text-xs text-muted-foreground">
          {t('costTrendEmpty')}
        </div>
      ) : (
        // recharts 는 SVG에 클릭/포커스 시 접근성용 포커스 링을 기본 표시한다.
        // 이 카드는 클릭 인터랙션이 없으므로(툴팁은 hover 로 충분) 시각적 잔상만
        // 남기지 않도록 아웃라인을 제거한다.
        // flex-1 로 카드 남은 높이를 전부 차지 — 옆 도넛 카드에 맞춰 늘어난 만큼
        // 차트가 같이 커진다. 단독 배치(분석 페이지)에서는 min-h 만큼만 잡힌다.
        <div className={`flex-1 [&_.recharts-wrapper]:outline-none [&_.recharts-wrapper_*]:outline-none [&_svg]:outline-none ${hasTeamSeries ? 'min-h-[232px]' : 'min-h-[200px]'}`}>
        <ResponsiveContainer width="100%" height="100%">
          <ComposedChart data={data} margin={{ top: 8, right: 8, left: -8, bottom: 0 }}>
            <defs>
              <linearGradient id="costFill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="hsl(var(--chart-1))" stopOpacity={0.28} />
                <stop offset="100%" stopColor="hsl(var(--chart-1))" stopOpacity={0} />
              </linearGradient>
            </defs>
            <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
            <XAxis
              dataKey="label"
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              minTickGap={24}
            />
            <YAxis
              yAxisId="cost"
              stroke="hsl(var(--muted-foreground))"
              fontSize={11}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v: number) => `$${v >= 1000 ? `${(v / 1000).toFixed(1)}k` : v}`}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: 'hsl(var(--card))',
                border: '1px solid hsl(var(--border))',
                borderRadius: '12px',
                fontSize: '12px',
              }}
              labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
              formatter={((value: number, name: string) => [
                `$${Number(value).toLocaleString('en-US', { maximumFractionDigits: 2 })}`,
                name,
              ]) as never}
            />
            {hasTeamSeries && (
              <Legend
                verticalAlign="bottom"
                height={28}
                iconSize={11}
                iconType="plainline"
                wrapperStyle={{ fontSize: '11px', paddingTop: '6px' }}
              />
            )}
            {/* type=linear + connectNulls 미설정: 실측이 없는 날은 곡선 보간도,
                gap 잇기도 하지 않는다 — 사용량이 없는 날짜는 있는 그대로 비어 보인다. */}
            <Area
              yAxisId="cost"
              type="linear"
              dataKey="total"
              name={t('costSeriesTotal')}
              stroke="hsl(var(--chart-1))"
              strokeWidth={3}
              fill="url(#costFill)"
            />
            {series.map((s) => (
              <Line
                key={s.dataKey}
                yAxisId="cost"
                type="linear"
                dataKey={s.dataKey}
                name={s.name}
                stroke={s.color}
                strokeWidth={1.8}
                strokeDasharray={s.dash}
                dot={false}
              />
            ))}
            {showOther && (
              <Line
                yAxisId="cost"
                type="linear"
                dataKey="other"
                name={t('costSeriesOther')}
                stroke="hsl(var(--muted-foreground))"
                strokeWidth={1.8}
                strokeDasharray="2 4"
                dot={false}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
