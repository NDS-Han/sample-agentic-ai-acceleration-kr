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
import { CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';
import { labelFor } from '@/lib/utils/modelLabel';

ChartJS.register(ArcElement, Tooltip, Legend);

// 항목 구분용 다색 카테고리 팔레트 (색맹 안전, 라이트/다크 공통).
const COLORS = CATEGORICAL_PALETTE;

interface Props {
  data: ClientShareResponse;
}

export function ClientShareDonutClient({ data }: Props) {
  const t = useTranslations('dashboard');
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
      // 호버한 조각의 우측 목록 행을 강조한다 — 캔버스 툴팁은 점유율이 큰
      // 조각에서 도넛 중앙(1위 % 오버레이)을 피할 방법이 없어 끈다.
      onHover: (_event: ChartEvent, elements: ActiveElement[]) => {
        setHoverIndex(elements.length ? elements[0].index : null);
      },
      plugins: {
        legend: { display: false },
        tooltip: { enabled: false },
      },
    }),
    [data],
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
            className={`flex items-center justify-between gap-2 text-sm rounded-apple-sm px-2 py-0.5 -mx-2 transition-colors ${i === hoverIndex ? 'bg-accent' : ''}`}
          >
            <div className="flex items-center gap-2 min-w-0">
              <span
                className="w-3 h-3 rounded-full flex-shrink-0"
                style={{ backgroundColor: COLORS[i % COLORS.length] }}
              />
              <span className="font-medium truncate">{labelFor(c.client)}</span>
            </div>
            <div className="flex items-center gap-3 text-xs shrink-0">
              <span className="text-muted-foreground tabular-nums">
                {t('callCount', { count: c.call_count })}
              </span>
              {c.web_search_count > 0 && (
                <span className="text-muted-foreground tabular-nums">
                  {t('webSearchCount', { count: c.web_search_count })}
                </span>
              )}
              <span className="tabular-nums">${c.cost_usd.toFixed(2)}</span>
              <span className="text-muted-foreground tabular-nums w-12 text-right">
                {c.share_pct.toFixed(1)}%
              </span>
            </div>
          </li>
        ))}
      </ul>
    </div>
  );
}
