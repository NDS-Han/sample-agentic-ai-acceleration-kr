'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


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
import { fmtUsd } from '@/lib/utils/format';
import type { ModelBreakdown, TeamBreakdown } from '@/types/entities';
import { CATEGORICAL_PALETTE, useChartTheme } from '@/lib/utils/chartTheme';

interface BreakdownChartClientProps {
  labels: string[];
  values: number[];
  title: string;
}

export function BreakdownChartClient({ labels, values, title }: BreakdownChartClientProps) {
  const t = useTranslations('analytics');
  const theme = useChartTheme();

  const points = labels.map((label, i) => ({ label, value: values[i] ?? 0 }));

  // 축 라벨은 -45° 회전 — 회전 텍스트의 수직 확장(≈길이·0.7)이 XAxis height 를
  // 넘으면 svg 경계에서 라벨 앞부분이 잘린다("Claude Sonnet 5.5" → "~laude …").
  // 표시는 20자로 자르고(전체 이름은 툴팁이 담당), 축 높이는 최대 표시 길이에
  // 맞춰 회전 라벨이 온전히 들어가게 한다.
  const TICK_MAX_CHARS = 20;
  const maxTickChars = Math.min(
    TICK_MAX_CHARS,
    points.reduce((m, p) => Math.max(m, p.label.length), 0),
  );
  const xAxisHeight = Math.max(70, 24 + maxTickChars * 4.2);

  if (points.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        {t('noData')}
      </div>
    );
  }

  const formatTick = (label: string) =>
    label.length > TICK_MAX_CHARS ? `${label.slice(0, TICK_MAX_CHARS - 1)}…` : label;

  return (
    <div>
      <div className="mb-2 text-center text-sm font-medium">{title}</div>
      <ResponsiveContainer width="100%" height={300}>
        <BarChart data={points} margin={{ top: 8, right: 8, left: 0, bottom: 0 }}>
          <CartesianGrid vertical={false} stroke={theme.grid} />
          <XAxis
            dataKey="label"
            // 라벨이 많으면 겹치지 않게 건너뛰며 표시 — 상세는 툴팁이 담당.
            interval="preserveStartEnd"
            minTickGap={24}
            angle={-45}
            textAnchor="end"
            height={xAxisHeight}
            tickFormatter={formatTick}
            tick={{ fontSize: 11, fill: theme.textMuted }}
            axisLine={{ stroke: theme.grid }}
            tickLine={false}
          />
          <YAxis
            tickFormatter={(v) => `$${Number(v).toFixed(2)}`}
            tick={{ fontSize: 11, fill: theme.textMuted }}
            axisLine={false}
            tickLine={false}
            width={64}
          />
          <Tooltip
            cursor={{ fill: 'rgba(127,127,127,0.10)' }}
            formatter={(value) => [fmtUsd(Number(value)), t('costUsd')]}
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
          {/* 막대마다 카테고리 색 — 항목 구분 또렷. */}
          <Bar dataKey="value" radius={[6, 6, 0, 0]}>
            {points.map((_, i) => (
              <Cell key={i} fill={CATEGORICAL_PALETTE[i % CATEGORICAL_PALETTE.length]} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <div className="mt-1 flex justify-between text-[10px] text-muted-foreground">
        <span>{t('item')}</span>
        <span>USD</span>
      </div>
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
