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

  if (points.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        {t('noData')}
      </div>
    );
  }

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
            height={70}
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
