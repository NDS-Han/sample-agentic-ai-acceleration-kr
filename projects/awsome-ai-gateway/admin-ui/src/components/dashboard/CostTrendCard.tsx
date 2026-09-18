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

interface TrendPoint {
  date: string;
  cost_usd: number;
  requests: number;
}

interface TeamTrendSeries {
  team: string;
  team_id: string;
  points: TrendPoint[];
}

interface CostTrendCardProps {
  trends: TrendPoint[];
  trendsByTeam?: TeamTrendSeries[];
}

// 기본으로 개별 표시할 팀 수 — 이만큼이면 범례 한 줄 + 선 구분이 유지된다.
const TOP_TEAM_COUNT = 5;

// 팀 시리즈 색상 — chart 토큰 우선, 이후는 팔레트 확장 보조색.
const TEAM_COLORS = [
  'hsl(var(--chart-2))',   // sky
  'hsl(var(--chart-3))',   // pink
  'hsl(var(--chart-4))',   // violet
  'hsl(var(--chart-5))',   // amber
  'hsl(350 85% 66%)',      // rose
  'hsl(150 65% 42%)',      // green
  'hsl(230 75% 66%)',      // indigo
  'hsl(185 75% 42%)',      // cyan
  'hsl(25 90% 55%)',       // orange
  'hsl(280 60% 60%)',      // purple
];

// 팀별 선형 — 색뿐 아니라 대시 패턴도 달리해 단색 출력/색약에도 구분되게.
const TEAM_DASHES = ['0', '8 4', '4 4', '10 4 2 4', '3 3', '12 4', '6 2 2 2', '2 5', '9 3 3 3', '5 5'];

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

interface RankedTeam {
  teamId: string;
  name: string;
  total: number;
  byDate: Map<string, number>;
}

function rankTeams(trendsByTeam: TeamTrendSeries[]): RankedTeam[] {
  return trendsByTeam
    .map((tt) => ({
      teamId: tt.team_id,
      name: tt.team,
      total: tt.points.reduce((s, p) => s + Number(p.cost_usd || 0), 0),
      byDate: new Map(tt.points.map((p) => [p.date, Number(p.cost_usd)])),
    }))
    .sort((a, b) => b.total - a.total);
}

/** 기본 선택 = 비용 상위 TOP_TEAM_COUNT 개 팀 id. */
export function defaultSelectedTeamIds(trendsByTeam: TeamTrendSeries[]): Set<string> {
  return new Set(rankTeams(trendsByTeam).slice(0, TOP_TEAM_COUNT).map((t) => t.teamId));
}

/**
 * 추이 차트 데이터 조립 — 합계 + 팀별 시리즈를 날짜 축으로 정렬한다.
 * 렌더와 무관한 순수 함수라 상위 N/기타 합산·날짜 정합성을 직접 검증할 수 있다.
 *
 * 규칙:
 *  - x축 날짜 = 합계 + 모든 팀 포인트의 날짜 합집합(오름차순). 어느 시리즈든
 *    그 날짜에 포인트가 없으면 null — 0 으로 접으면 "그날 비용 0" 이라는 거짓
 *    사실이 되므로 connectNulls 로 끊어 그린다.
 *  - selectedIds 에 든 팀만 개별 시리즈. 나머지 팀은 날짜별 합산 'other'
 *    시리즈 하나로 접는다 — 꺼둔 팀의 비용이 화면에서 사라지지 않는다.
 *  - selectedIds 미지정이면 비용 상위 TOP_TEAM_COUNT 개가 기본 선택이다.
 */
export function buildTrendChartData(
  trends: TrendPoint[],
  trendsByTeam: TeamTrendSeries[],
  selectedIds?: ReadonlySet<string>,
): TrendChartData {
  const ranked = rankTeams(trendsByTeam);
  const selected = selectedIds ?? new Set(ranked.slice(0, TOP_TEAM_COUNT).map((t) => t.teamId));

  const totalByDate = new Map(trends.map((p) => [p.date, Number(p.cost_usd)]));
  const dateSet = new Set<string>(totalByDate.keys());
  for (const s of ranked) for (const d of s.byDate.keys()) dateSet.add(d);
  const dates = [...dateSet].sort();

  const on = ranked.filter((t) => selected.has(t.teamId));
  const off = ranked.filter((t) => !selected.has(t.teamId));

  const data = dates.map((d) => {
    const row: Record<string, number | string | null> = {
      label: d.length >= 10 ? d.slice(5) : d,
      total: totalByDate.get(d) ?? null,
    };
    for (const s of on) row[`team_${s.teamId}`] = s.byDate.get(d) ?? null;
    if (off.length > 0) {
      row.other = off.reduce((s, tt) => s + (tt.byDate.get(d) ?? 0), 0);
    }
    return row;
  });

  const allTeams = ranked.map((t, i) => ({
    dataKey: `team_${t.teamId}`,
    teamId: t.teamId,
    name: t.name,
    color: TEAM_COLORS[i % TEAM_COLORS.length],
    dash: TEAM_DASHES[i % TEAM_DASHES.length],
    total: t.total,
  }));
  const series = allTeams.filter((t) => selected.has(t.teamId));

  return {
    data,
    series,
    allTeams,
    showOther: off.length > 0,
    hasTeamSeries: ranked.length > 0,
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
    <div className="glass glass-hover rounded-apple p-5">
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
                onClick={() => toggleTeam(tm.teamId)}
                className={`inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[11px] transition-colors ${
                  on
                    ? 'border-transparent bg-secondary text-foreground'
                    : 'border-border text-muted-foreground hover:bg-secondary/60'
                }`}
              >
                <span
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: on ? tm.color : 'hsl(var(--muted-foreground))' }}
                />
                {tm.name}
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
        <div className="[&_.recharts-wrapper]:outline-none [&_.recharts-wrapper_*]:outline-none [&_svg]:outline-none">
        <ResponsiveContainer width="100%" height={hasTeamSeries ? 232 : 200}>
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
                verticalAlign="top"
                height={30}
                iconSize={11}
                iconType="plainline"
                wrapperStyle={{ fontSize: '11px' }}
              />
            )}
            <Area
              yAxisId="cost"
              type="monotone"
              dataKey="total"
              name={t('costSeriesTotal')}
              stroke="hsl(var(--chart-1))"
              strokeWidth={3}
              fill="url(#costFill)"
              connectNulls
            />
            {series.map((s) => (
              <Line
                key={s.dataKey}
                yAxisId="cost"
                type="monotone"
                dataKey={s.dataKey}
                name={s.name}
                stroke={s.color}
                strokeWidth={1.8}
                strokeDasharray={s.dash}
                dot={false}
                connectNulls
              />
            ))}
            {showOther && (
              <Line
                yAxisId="cost"
                type="monotone"
                dataKey="other"
                name={t('costSeriesOther')}
                stroke="hsl(var(--muted-foreground))"
                strokeWidth={1.8}
                strokeDasharray="2 4"
                dot={false}
                connectNulls
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
