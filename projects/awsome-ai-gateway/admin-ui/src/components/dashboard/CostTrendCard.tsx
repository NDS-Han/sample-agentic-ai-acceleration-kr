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
} from 'recharts';
import { fmtUsdExact, fmtUsdTick, niceTicks } from '@/lib/utils/format';
import {
  buildTrendSeries,
  defaultSelectedTeamIds,
  TEAM_COLORS,
  type TeamSeriesMeta,
  type TeamTrendSeries,
  type TrendPoint,
  type TrendRange,
} from '@/lib/utils/trendSeries';
import { reportingNowParts } from '@/lib/utils/period';
import { useReportingTz } from '@/components/common/ReportingTimezoneProvider';

export { defaultSelectedTeamIds };

interface CostTrendCardProps {
  trends: TrendPoint[];
  trendsByTeam?: TeamTrendSeries[];
  /** 조회 기간 — 주면 축을 기간 전체로 넓히고 사용 없는 날을 $0 으로 잇는다. */
  range?: TrendRange;
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
  opts: { range?: TrendRange; today?: string } = {},
): TrendChartData {
  const { dates, total, series, allTeams, other, hasTeamSeries } =
    buildTrendSeries(trends, trendsByTeam, selectedIds, TEAM_COLORS, opts);

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

/**
 * 앞뒤 날짜가 모두 null 인 단독 실측점 — 선이 그어지지 않아(connectNulls 없음)
 * 아예 안 보이므로 그 점만 dot 으로 찍는다. 이어지는 구간엔 dot 을 두지 않는다.
 */
function isolatedDot(
  data: TrendChartData['data'],
  key: string,
  color: string,
) {
  // recharts 가 dot 함수에 넘기는 props 중 쓰는 것만.
  return function IsolatedDot(props: { cx?: number; cy?: number; index?: number }) {
    const { cx, cy, index = 0 } = props;
    const v = data[index]?.[key];
    const lone = v != null && data[index - 1]?.[key] == null && data[index + 1]?.[key] == null;
    if (!lone || cx == null || cy == null) return <g key={`${key}-${index}`} />;
    return <circle key={`${key}-${index}`} cx={cx} cy={cy} r={3} fill={color} stroke="none" />;
  };
}

/** 범례 견본 — 시리즈의 색과 대시 패턴을 그대로 보여준다(색약/단색 출력 구분). */
function LineSwatch({ color, dash, width = 2 }: { color: string; dash?: string; width?: number }) {
  return (
    <svg width="16" height="6" aria-hidden="true" className="shrink-0">
      <line x1="0" y1="3" x2="16" y2="3" stroke={color} strokeWidth={width} strokeDasharray={dash} />
    </svg>
  );
}

export function CostTrendCard({ trends, trendsByTeam = [], range }: CostTrendCardProps) {
  const t = useTranslations('dashboard');
  const tz = useReportingTz();
  // 리포팅 TZ 기준 오늘 — 이후 날짜는 그리지 않는다(버킷 날짜와 같은 기준).
  const today = useMemo(() => {
    const { y, m, d } = reportingNowParts(tz);
    return `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`;
  }, [tz]);
  const topIds = useMemo(() => defaultSelectedTeamIds(trendsByTeam), [trendsByTeam]);
  // null = 기본 선택(상위 N). 사용자가 칩을 누르면 명시 선택이 된다.
  const [picked, setPicked] = useState<Set<string> | null>(null);
  const effective = picked ?? topIds;
  const { data, series, allTeams, showOther, hasTeamSeries } = useMemo(
    () => buildTrendChartData(trends, trendsByTeam, effective, { range, today }),
    [trends, trendsByTeam, effective, range, today],
  );

  // nice ticks — 기본 균등 분할은 $0.135, $0.09 같은 어중간한 눈금이 나온다.
  // 켜진 시리즈(합계·팀·기타) 최댓값을 덮는 1·2·2.5·5×10ⁿ 간격으로 고정.
  const yTicks = useMemo(() => {
    let max = 0;
    for (const row of data) {
      for (const [k, v] of Object.entries(row)) {
        if (k !== 'label' && typeof v === 'number' && v > max) max = v;
      }
    }
    return niceTicks(max);
  }, [data]);
  const yStep = yTicks.length > 1 ? yTicks[1] - yTicks[0] : 1;

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

      {/* 범례는 이 칩 줄 하나 — 예전엔 차트 아래 recharts Legend 가 같은 팀을
          한 번 더 나열했다. 합계·기타는 토글 대상이 아니라 정적 항목으로 둔다. */}
      {hasTeamSeries && data.length > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-1.5">
          <span className="inline-flex items-center gap-1.5 px-1 py-1 text-[11px] text-foreground">
            <LineSwatch color="hsl(var(--chart-1))" width={3} />
            {t('costSeriesTotal')}
          </span>
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
                <LineSwatch color={on ? tm.color : 'hsl(var(--muted-foreground))'} dash={tm.dash} />
                <span className="max-w-[12rem] truncate">{tm.name}</span>
              </button>
            );
          })}
          {showOther && (
            <span className="inline-flex items-center gap-1.5 px-1 py-1 text-[11px] text-muted-foreground">
              <LineSwatch color="hsl(var(--muted-foreground))" dash="2 4" />
              {t('costSeriesOther')}
            </span>
          )}
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
        <div className="flex-1 min-h-[200px] [&_.recharts-wrapper]:outline-none [&_.recharts-wrapper_*]:outline-none [&_svg]:outline-none">
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
              ticks={yTicks}
              domain={[0, yTicks[yTicks.length - 1]]}
              tickFormatter={(v: number) => fmtUsdTick(v, yStep)}
            />
            <Tooltip
              contentStyle={{
                backgroundColor: 'hsl(var(--card))',
                border: '1px solid hsl(var(--border))',
                borderRadius: '12px',
                fontSize: '12px',
              }}
              labelStyle={{ color: 'hsl(var(--muted-foreground))' }}
              formatter={((value: number, name: string) => [fmtUsdExact(Number(value)), name]) as never}
            />
            {/* type=linear + connectNulls 미설정: 사용 없는 날은 $0 으로 이어 그리고,
                오늘 이후(null)만 비운다. */}
            <Area
              yAxisId="cost"
              type="linear"
              dataKey="total"
              name={t('costSeriesTotal')}
              stroke="hsl(var(--chart-1))"
              strokeWidth={3}
              fill="url(#costFill)"
              dot={isolatedDot(data, 'total', 'hsl(var(--chart-1))')}
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
                dot={isolatedDot(data, s.dataKey, s.color)}
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
                dot={isolatedDot(data, 'other', 'hsl(var(--muted-foreground))')}
              />
            )}
          </ComposedChart>
        </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}
