'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Cell, Pie, PieChart } from 'recharts';

export interface TokenBreakdownData {
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  total_tokens: number;
}

// 버킷별 의미색 — 팔레트 순환색과 달리 어떤 차트에서도 같은 버킷=같은 색.
// sky(#38bdf8)와 teal(#2dd4bf)은 색상축이 붙어 있어 구분이 안 된다 — 4개가
// 전부 다른 색상축이 되게 input=sky / output=pink / read=green / write=amber.
const BUCKETS = [
  { key: 'input_tokens', labelKey: 'tokenInput', color: '#38bdf8' },
  { key: 'output_tokens', labelKey: 'tokenOutput', color: '#f472b6' },
  { key: 'cache_read_tokens', labelKey: 'tokenCacheRead', color: '#4ade80' },
  { key: 'cache_write_tokens', labelKey: 'tokenCacheWrite', color: '#fbbf24' },
] as const;

const compact = new Intl.NumberFormat('en', { notation: 'compact', maximumFractionDigits: 1 });

interface Props {
  data: TokenBreakdownData;
}

export function TokenMixDonutClient({ data }: Props) {
  const t = useTranslations('analytics');
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);

  const items = useMemo(
    () =>
      BUCKETS.map((b) => ({
        ...b,
        label: t(b.labelKey),
        tokens: data[b.key],
        sharePct: data.total_tokens > 0 ? (data[b.key] / data.total_tokens) * 100 : 0,
      })),
    [data, t],
  );
  // 기본 중앙 표시는 최대 버킷 — cost share 도넛들과 같은 규칙.
  const topIndex = items.reduce((top, it, i) => (it.tokens > items[top].tokens ? i : top), 0);

  return (
    <div className="flex flex-col md:flex-row items-center justify-center gap-6 md:gap-10">
      <div className="relative h-64 w-64 shrink-0">
        {/* 캔버스 툴팁은 도넛 중앙 오버레이를 가린다 — 호버 상세는 중앙 표시가 담당.
            activeShape 로 호버 조각이 바깥으로 튀어나온다(기존 hoverOffset 대응). */}
        <PieChart width={256} height={256}>
          <Pie
            data={items}
            dataKey="tokens"
            nameKey="label"
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
            {items.map((it) => (
              <Cell key={it.key} fill={it.color} />
            ))}
          </Pie>
        </PieChart>
        {/* 가운데: 기본은 최대 버킷, 조각 호버 시에는 그 버킷의 수치로 바뀐다 — 툴팁을 대체. */}
        {(() => {
          const centerItem = items[hoverIndex ?? topIndex];
          return (
            <div className="absolute inset-0 flex flex-col items-center justify-center px-6 text-center pointer-events-none">
              {centerItem && (
                <span
                  className="mb-1 h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: centerItem.color }}
                  aria-hidden="true"
                />
              )}
              <p className="text-2xl font-bold leading-none tracking-tight">
                {centerItem ? `${centerItem.sharePct.toFixed(1)}%` : '—'}
              </p>
              <p className="mt-1 max-w-full truncate text-xs font-medium text-foreground">
                {centerItem?.label ?? ''}
              </p>
              <p className="mt-0.5 text-[10px] text-muted-foreground tabular-nums">
                {hoverIndex != null && centerItem
                  ? compact.format(centerItem.tokens)
                  : t('tokenTotal', { total: compact.format(data.total_tokens) })}
              </p>
            </div>
          );
        })()}
      </div>
      <ul className="space-y-2 w-full md:w-[24rem]">
        {items.map((it, i) => (
          <li
            key={it.key}
            // 이름 + 고정폭 우측정렬 수치 컬럼 — 행마다 수치가 세로로 정렬된다.
            className={`grid grid-cols-[minmax(0,1fr)_5.5rem_4rem] items-baseline gap-2 text-sm rounded-apple-sm px-2 py-0.5 -mx-2 transition-colors ${i === hoverIndex ? 'bg-accent' : ''}`}
          >
            <div className="flex items-start gap-2 min-w-0">
              <span
                className="w-3 h-3 rounded-full flex-shrink-0 mt-1"
                style={{ backgroundColor: it.color }}
              />
              <span className="font-medium truncate">{it.label}</span>
            </div>
            <span
              className="tabular-nums text-xs text-right"
              title={it.tokens.toLocaleString()}
            >
              {compact.format(it.tokens)}
            </span>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {it.sharePct.toFixed(1)}%
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
