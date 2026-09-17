'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import {
  Chart as ChartJS,
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend,
} from 'chart.js';
import { Line } from 'react-chartjs-2';
import { useTranslations } from 'next-intl';
import type { TrendDataPoint } from '@/types/entities';
import { PRIMARY_SERIES, useChartTheme } from '@/lib/utils/chartTheme';

ChartJS.register(
  CategoryScale,
  LinearScale,
  PointElement,
  LineElement,
  Title,
  Tooltip,
  Legend
);

interface CostTrendChartClientProps {
  trends: TrendDataPoint[];
}

export function CostTrendChartClient({ trends }: CostTrendChartClientProps) {
  const t = useTranslations('analytics');
  const theme = useChartTheme();
  const labels = trends.map((d) => d.date);
  const values = trends.map((d) => d.cost_usd);

  const data = {
    labels,
    datasets: [
      {
        label: t('costUsd'),
        data: values,
        borderColor: PRIMARY_SERIES,
        backgroundColor: 'rgba(45, 212, 191, 0.16)',
        fill: true,
        tension: 0.3,
        pointRadius: 3,
        pointHoverRadius: 5,
      },
    ],
  };

  const options = {
    responsive: true,
    plugins: {
      legend: { position: 'top' as const, labels: { color: theme.text } },
      title: { display: true, text: t('usageTrend'), color: theme.text },
      tooltip: {
        callbacks: {
          label: (ctx: import('chart.js').TooltipItem<'line'>) =>
            `$${(ctx.parsed.y ?? 0).toFixed(4)}`,
        },
      },
    },
    scales: {
      x: {
        title: { display: true, text: t('date'), color: theme.textMuted },
        ticks: { color: theme.textMuted },
        grid: { color: theme.grid },
      },
      y: {
        title: { display: true, text: 'USD', color: theme.textMuted },
        ticks: {
          color: theme.textMuted,
          callback: (value: string | number) => `$${Number(value).toFixed(2)}`,
        },
        grid: { color: theme.grid },
      },
    },
  };

  if (trends.length === 0) {
    return (
      <div className="flex items-center justify-center h-48 text-sm text-muted-foreground">
        {t('noData')}
      </div>
    );
  }

  return <Line data={data} options={options} />;
}