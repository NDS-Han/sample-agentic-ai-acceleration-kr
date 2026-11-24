'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { useRef, type KeyboardEvent } from 'react';
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from 'recharts';
import { useTranslations } from 'next-intl';
import { fmtUsdExact, fmtUsdTick, niceTicks } from '@/lib/utils/format';
import type { ModelBreakdown, TeamBreakdown } from '@/types/entities';
import { CATEGORICAL_PALETTE, useChartTheme } from '@/lib/utils/chartTheme';
import { requestUserModelDrawer, type UserModelTarget } from './userModel';

interface BreakdownChartClientProps {
  labels: string[];
  values: number[];
  title: string;
  /** By User 모드 — labels 와 같은 순서. 있으면 막대·이름이 사용자×모델 상세를 연다. */
  userTargets?: UserModelTarget[];
}

export function BreakdownChartClient({ labels, values, title, userTargets }: BreakdownChartClientProps) {
  const t = useTranslations('analytics');
  const tu = useTranslations('analytics.userModel');
  const theme = useChartTheme();
  const rootRef = useRef<HTMLDivElement>(null);
  const clickable = !!userTargets && userTargets.length > 0;

  // 막대 클릭엔 포커스 가능한 요소가 없다 — 같은 행의 이름(포커스 가능한 tick)을
  // 트리거로 넘겨 드로어가 닫힐 때 거기로 포커스를 돌려준다.
  const openUser = (index: number) => {
    const target = userTargets?.[index];
    if (!target) return;
    const trigger = rootRef.current?.querySelector<HTMLElement>(`[data-user-index="${index}"]`) ?? null;
    requestUserModelDrawer({ ...target, trigger });
  };

  const points = labels.map((label, i) => ({ label, value: values[i] ?? 0 }));

  if (points.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        {t('noData')}
      </div>
    );
  }

  // 가로 막대(layout="vertical") — 세로 막대의 -45° 회전 라벨은 모델명이 잘렸다
  // ("Claude Sonnet 5.5" → "~laude …"). 이름을 Y축에 가로로 두면 전체가 보인다.
  // Y축 폭은 가장 긴 이름에 맞추되(11px ≈ 글자당 6.4px) 카드 폭을 다 먹지 않게
  // 상한을 둔다 — 상한을 넘는 이름만 말줄임, 전체 이름은 툴팁.
  const LABEL_MAX_CHARS = 36;
  const maxChars = Math.min(
    LABEL_MAX_CHARS,
    points.reduce((m, p) => Math.max(m, p.label.length), 0),
  );
  const yAxisWidth = Math.max(72, Math.round(maxChars * 6.4) + 12);
  const formatLabel = (label: string) =>
    label.length > LABEL_MAX_CHARS ? `${label.slice(0, LABEL_MAX_CHARS - 1)}…` : label;
  // 막대 하나당 고정 높이 — 항목 수에 따라 차트가 늘어나 라벨을 건너뛰지 않는다.
  const chartHeight = Math.max(160, points.length * 32 + 40);

  const ticks = niceTicks(points.reduce((m, p) => Math.max(m, p.value), 0));
  const step = ticks.length > 1 ? ticks[1] - ticks[0] : 1;

  // By User 모드의 이름 tick — 키보드(Enter/Space)로도 열리도록 버튼 역할.
  const renderUserTick = (props: {
    x?: number | string;
    y?: number | string;
    index?: number;
    payload?: { value?: string; index?: number };
  }) => {
    const index = props.index ?? props.payload?.index ?? 0;
    const label = String(props.payload?.value ?? '');
    const onKey = (e: KeyboardEvent<SVGGElement>) => {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        openUser(index);
      }
    };
    return (
      <g
        data-user-index={index}
        role="button"
        tabIndex={0}
        aria-label={tu('openBreakdown', { name: label })}
        onClick={() => openUser(index)}
        onKeyDown={onKey}
        className="cursor-pointer outline-none focus-visible:[&>text]:underline"
      >
        <text
          x={Number(props.x ?? 0)}
          y={Number(props.y ?? 0)}
          dy={4}
          textAnchor="end"
          fontSize={11}
          fill={theme.textMuted}
        >
          {formatLabel(label)}
        </text>
      </g>
    );
  };

  return (
    <div ref={rootRef}>
      <div className="mb-2 text-center text-sm font-medium">{title}</div>
      {clickable && (
        <div className="-mt-1 mb-2 text-center text-xs text-primary">{tu('chartHint')}</div>
      )}
      <ResponsiveContainer width="100%" height={chartHeight}>
        <BarChart
          data={points}
          layout="vertical"
          margin={{ top: 4, right: 16, left: 0, bottom: 0 }}
        >
          <CartesianGrid horizontal={false} stroke={theme.grid} />
          <XAxis
            type="number"
            ticks={ticks}
            domain={[0, ticks[ticks.length - 1]]}
            tickFormatter={(v) => fmtUsdTick(Number(v), step)}
            tick={{ fontSize: 11, fill: theme.textMuted }}
            axisLine={{ stroke: theme.grid }}
            tickLine={false}
          />
          <YAxis
            type="category"
            dataKey="label"
            interval={0}
            width={yAxisWidth}
            tickFormatter={clickable ? undefined : formatLabel}
            tick={clickable ? renderUserTick : { fontSize: 11, fill: theme.textMuted }}
            axisLine={false}
            tickLine={false}
          />
          <Tooltip
            cursor={{ fill: 'rgba(127,127,127,0.10)' }}
            formatter={(value) => [fmtUsdExact(Number(value)), t('costUsd')]}
            contentStyle={{
              background: theme.surface,
              border: '1px solid hsl(var(--border))',
              borderRadius: 8,
              fontSize: 12,
            }}
            // 다크에서 기본 항목 텍스트가 검정으로 남아 배경과 충돌한다 —
            // 라벨/값 모두 테마 해석값으로 명시한다.
            labelStyle={{ color: theme.text }}
            itemStyle={{ color: theme.text }}
          />
          {/* 막대마다 카테고리 색 — 항목 구분 또렷. 축 눈금이 $ 단위라
              예전 하단의 'Item'/'USD' 모서리 라벨은 두지 않는다. */}
          <Bar
            dataKey="value"
            radius={[0, 6, 6, 0]}
            barSize={18}
            onClick={clickable ? (_data, index) => openUser(index) : undefined}
            cursor={clickable ? 'pointer' : undefined}
          >
            {points.map((_, i) => (
              <Cell key={i} fill={CATEGORICAL_PALETTE[i % CATEGORICAL_PALETTE.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// 모델별 breakdown용 타입 헬퍼
export function modelBreakdownToChartProps(breakdowns: ModelBreakdown[]) {
  return {
    labels: breakdowns.map((b) => b.model_alias),
    values: breakdowns.map((b) => b.cost_usd),
    title: '모델별 비용 분석',
  };
}

// 팀별 breakdown용 타입 헬퍼
export function teamBreakdownToChartProps(breakdowns: TeamBreakdown[]) {
  return {
    labels: breakdowns.map((b) => b.team_name),
    values: breakdowns.map((b) => b.cost_usd),
    title: '팀별 비용 분석',
  };
}
