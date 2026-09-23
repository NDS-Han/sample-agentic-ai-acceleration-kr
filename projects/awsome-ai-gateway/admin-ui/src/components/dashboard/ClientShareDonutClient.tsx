'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import {
  Chart as ChartJS,
  ArcElement,
  Tooltip,
  Legend,
} from 'chart.js';
import type { ActiveElement, ChartEvent } from 'chart.js';
import { Doughnut } from 'react-chartjs-2';
import type { ClientShareResponse } from '@/lib/actions/dashboard';
import { CATEGORICAL_PALETTE, useChartTheme } from '@/lib/utils/chartTheme';
import { labelFor } from '@/lib/utils/modelLabel';

ChartJS.register(ArcElement, Tooltip, Legend);

// 항목 구분용 다색 카테고리 팔레트 (색맹 안전, 라이트/다크 공통).
const COLORS = CATEGORICAL_PALETTE;

interface Props {
  data: ClientShareResponse;
}

export function ClientShareDonutClient({ data }: Props) {
  const t = useTranslations('dashboard');
  const theme = useChartTheme();
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const chartData = useMemo(
    () => ({
      labels: data.clients.map((c) => labelFor(c.client)),
      datasets: [
        {
          data: data.clients.map((c) => c.cost_usd),
          backgroundColor: data.clients.map((_, i) => COLORS[i % COLORS.length]),
          borderWidth: 0,
          spacing: 0,
          hoverOffset: 14,
        },
      ],
    }),
    [data],
  );

  const options = useMemo(
    () => ({
      responsive: true,
      maintainAspectRatio: false,
      cutout: '62%',
      animation: { animateRotate: true, animateScale: false },
      // 호버한 조각의 우측 목록 행도 함께 강조한다.
      onHover: (_event: ChartEvent, elements: ActiveElement[]) => {
        setHoverIndex(elements.length ? elements[0].index : null);
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          // 기본 rgba(0,0,0,0.8) 은 반투명이라 뒤의 중앙 오버레이가 비쳐
          // '겹침' 으로 보였다. 카드 표면색(불투명)+테두리의 팝오버로 바꾼다.
          backgroundColor: theme.surface,
          titleColor: theme.text,
          bodyColor: theme.textMuted,
          borderColor: theme.isDark ? 'rgba(255,255,255,0.16)' : 'rgba(15,23,42,0.12)',
          borderWidth: 1,
          padding: 10,
          cornerRadius: 8,
          position: 'nearest' as const,
          callbacks: {
            // Use the backend-computed share_pct (authoritative) rather than
            // recomputing from cost/total, which can drift due to rounding.
            label: (ctx: import('chart.js').TooltipItem<'doughnut'>) => {
              const item = data.clients[ctx.dataIndex];
              if (!item) return '';
              return `${labelFor(item.client)}: $${item.cost_usd.toFixed(2)} (${item.share_pct.toFixed(1)}%)`;
            },
            // 호출 수 + 서버측 웹검색 수(attribution 지표)를 같은 툴팁에 함께 표시.
            afterLabel: (ctx: import('chart.js').TooltipItem<'doughnut'>) => {
              const item = data.clients[ctx.dataIndex];
              if (!item) return '';
              const lines = [t('callCount', { count: item.call_count })];
              if (item.web_search_count) lines.push(t('webSearchCount', { count: item.web_search_count }));
              return lines;
            },
          },
        },
      },
    }),
    [data, theme, t],
  );

  if (!data.clients.length) {
    return (
      <div className="flex items-center justify-center h-64 text-sm text-muted-foreground">
        {t('noUsageForPeriod')}
      </div>
    );
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-6 items-center">
      <div className="relative h-64">
        <Doughnut data={chartData} options={options} />
        {/* 가운데: 점유율 1위 앱 강조 (clients 는 비용 desc 정렬, [0]=1위) */}
        <div className="absolute inset-0 flex flex-col items-center justify-center px-6 text-center pointer-events-none">
          {data.clients[0] && (
            <span
              className="mb-1 h-2.5 w-2.5 rounded-full"
              style={{ backgroundColor: COLORS[0] }}
              aria-hidden="true"
            />
          )}
          <p className="text-2xl font-bold leading-none tracking-tight">
            {data.clients[0] ? `${data.clients[0].share_pct.toFixed(0)}%` : '—'}
          </p>
          <p className="mt-1 max-w-full truncate text-xs font-medium text-foreground">
            {data.clients[0] ? labelFor(data.clients[0].client) : ''}
          </p>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            {t('topShare', { total: data.total_cost_usd.toLocaleString('en-US', {
              minimumFractionDigits: 2,
              maximumFractionDigits: 2,
            }) })}
          </p>
        </div>
      </div>
      <ul className="space-y-2">
        {data.clients.map((c, i) => (
          <li
            key={c.client}
            // 이름 + 고정폭 우측정렬 수치 컬럼 — justify-between 은 이름과 수치가
            // 양끝으로 벌어지고 행마다 수치 위치가 들쭉날쭉해 산만해 보였다.
            className={`grid grid-cols-[minmax(0,1fr)_5rem_6.5rem_5rem_3.5rem] items-center gap-2 text-sm rounded-apple-sm px-2 py-0.5 -mx-2 transition-colors ${i === hoverIndex ? 'bg-accent' : ''}`}
          >
            <div className="flex items-center gap-2 min-w-0">
              <span
                className="w-3 h-3 rounded-full flex-shrink-0"
                style={{ backgroundColor: COLORS[i % COLORS.length] }}
              />
              <span className="font-medium truncate">{labelFor(c.client)}</span>
            </div>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {t('callCount', { count: c.call_count })}
            </span>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {c.web_search_count > 0 ? t('webSearchCount', { count: c.web_search_count }) : ''}
            </span>
            <span className="tabular-nums text-xs text-right">${c.cost_usd.toFixed(2)}</span>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {c.share_pct.toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
