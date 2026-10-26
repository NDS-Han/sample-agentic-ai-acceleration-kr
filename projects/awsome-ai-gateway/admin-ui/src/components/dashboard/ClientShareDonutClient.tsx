'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Cell, Pie, PieChart } from 'recharts';
import { fmtUsd } from '@/lib/utils/format';
import type { ClientShareResponse } from '@/lib/actions/dashboard';
import { CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';
import { labelFor } from '@/lib/utils/modelLabel';

// 항목 구분용 다색 카테고리 팔레트 (색맹 안전, 라이트/다크 공통).
const COLORS = CATEGORICAL_PALETTE;

interface Props {
  data: ClientShareResponse;
}

export function ClientShareDonutClient({ data }: Props) {
  const t = useTranslations('dashboard');
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const pieData = useMemo(
    () =>
      data.clients.map((c, i) => ({
        name: labelFor(c.client),
        value: c.cost_usd,
        fill: COLORS[i % COLORS.length],
      })),
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
    // 카드가 전체 폭이라 grid-cols-2 는 도넛과 목록을 양끝으로 벌린다.
    // 도넛 고정폭 + 목록 컴팩트 컬럼을 중앙 정렬로 묶는다.
    <div className="flex flex-col md:flex-row items-center justify-center gap-6 md:gap-10">
      <div className="relative h-64 w-64 shrink-0">
        <PieChart width={256} height={256}>
          <Pie
            data={pieData}
            dataKey="value"
            nameKey="name"
            innerRadius="62%"
            outerRadius="98%"
            stroke="none"
            paddingAngle={0}
            startAngle={90}
            endAngle={-270}
            onMouseEnter={(_, i) => setHoverIndex(i)}
            onMouseLeave={() => setHoverIndex(null)}
            isAnimationActive
          >
            {pieData.map((p, i) => (
              <Cell key={i} fill={p.fill} />
            ))}
          </Pie>
        </PieChart>
        {/* 가운데: 기본은 점유율 1위(clients 는 비용 desc 정렬, [0]=1위),
            조각 호버 시에는 그 조각의 수치로 바뀐다 — 툴팁을 대체. */}
        {(() => {
          const centerItem = (hoverIndex != null ? data.clients[hoverIndex] : null) ?? data.clients[0];
          const centerIdx = hoverIndex ?? 0;
          return (
            <div className="absolute inset-0 flex flex-col items-center justify-center px-6 text-center pointer-events-none">
              {centerItem && (
                <span
                  className="mb-1 h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: COLORS[centerIdx % COLORS.length] }}
                  aria-hidden="true"
                />
              )}
              <p className="text-2xl font-bold leading-none tracking-tight">
                {centerItem ? `${centerItem.share_pct.toFixed(0)}%` : '—'}
              </p>
              <p className="mt-1 max-w-full truncate text-xs font-medium text-foreground">
                {centerItem ? labelFor(centerItem.client) : ''}
              </p>
              <p className="mt-0.5 text-[10px] text-muted-foreground tabular-nums">
                {hoverIndex != null && centerItem
                  ? fmtUsd(centerItem.cost_usd)
                  : t('topShare', { total: data.total_cost_usd.toLocaleString('en-US', {
                      minimumFractionDigits: 2,
                      maximumFractionDigits: 2,
                    }) })}
              </p>
            </div>
          );
        })()}
      </div>
      <ul className="space-y-2 w-full md:w-[30rem]">
        {data.clients.map((c, i) => (
          <li
            key={c.client}
            // 이름 + 고정폭 우측정렬 수치 컬럼 — justify-between 은 이름과 수치가
            // 양끝으로 벌어지고 행마다 수치 위치가 들쭉날쭉해 산만해 보였다.
            className={`grid grid-cols-[minmax(0,1fr)_5rem_6.5rem_5rem_3.5rem] items-baseline gap-2 text-sm rounded-apple-sm px-2 py-0.5 -mx-2 transition-colors ${i === hoverIndex ? 'bg-accent' : ''}`}
          >
            <div className="flex items-start gap-2 min-w-0">
              <span
                className="w-3 h-3 rounded-full flex-shrink-0 mt-1"
                style={{ backgroundColor: COLORS[i % COLORS.length] }}
              />
              {/* ModelShareDonut 과 동일 — 하이픈 토큰 중간 줄바꿈 방지(L5). */}
              <span className="font-medium break-words leading-snug" title={labelFor(c.client)}>
                {labelFor(c.client).split(' ').map((tok, ti) => (
                  <span key={ti} className={tok.includes('-') ? 'whitespace-nowrap' : undefined}>
                    {tok}{ti < labelFor(c.client).split(' ').length - 1 ? ' ' : ''}
                  </span>
                ))}
              </span>
            </div>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {t('callCount', { count: c.call_count })}
            </span>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {c.web_search_count > 0 ? t('webSearchCount', { count: c.web_search_count }) : ''}
            </span>
            <span className="tabular-nums text-xs text-right">{fmtUsd(c.cost_usd)}</span>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {c.share_pct.toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
