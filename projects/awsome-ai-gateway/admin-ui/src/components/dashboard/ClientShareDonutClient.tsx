'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { memo, useMemo, useState } from 'react';
import { useTranslations } from 'next-intl';
import { Cell, Pie, PieChart } from 'recharts';
import { fmtSharePct, fmtUsd, roundSharesTo100, usdTitle } from '@/lib/utils/format';
import type { ClientShareResponse } from '@/lib/actions/dashboard';
import { CATEGORICAL_PALETTE } from '@/lib/utils/chartTheme';
import { labelFor } from '@/lib/utils/modelLabel';
import { Usd } from '@/components/common/Usd';
import { WebSearchCostValue } from '@/components/common/WebSearchCost';
import { webSearchVisible } from '@/lib/utils/webSearchVisibility';

// 항목 구분용 다색 카테고리 팔레트 (색맹 안전, 라이트/다크 공통).
const COLORS = CATEGORICAL_PALETTE;

interface Props {
  data: ClientShareResponse;
}

// 호버는 부모 state 라 조각 위에 올라갈 때마다 이 컴포넌트가 리렌더된다 —
// Pie props 가 새 참조로 보이면 recharts 애니메이션이 도중에 멈춰 도넛이
// 반만 채워진 채 얼어붙는다. data/onHover 가 안정적이면 memo 가 호버
// 리렌더에서 이 서브트리를 건너뛰어 애니메이션이 끝까지 돈다.
export const ShareDonut = memo(function ShareDonut({
  data,
  onHover,
  size = 256,
}: {
  data: { name: string; value: number; fill: string }[];
  onHover: (_index: number | null) => void;
  /** 정사각 px — 감싸는 박스(h-/w-)와 반드시 같게. recharts PieChart 는 고정 크기라
      박스가 더 작으면 옆 범례 위로 넘쳐 그린다. */
  size?: number;
}) {
  return (
    <PieChart width={size} height={size}>
      <Pie
        data={data}
        dataKey="value"
        nameKey="name"
        innerRadius="62%"
        outerRadius="98%"
        stroke="none"
        paddingAngle={0}
        startAngle={90}
        endAngle={-270}
        onMouseEnter={(_, i) => onHover(i)}
        onMouseLeave={() => onHover(null)}
        isAnimationActive
      >
        {data.map((p, i) => (
          <Cell key={i} fill={p.fill} />
        ))}
      </Pie>
    </PieChart>
  );
});

export function ClientShareDonutClient({ data }: Props) {
  const t = useTranslations('dashboard');
  // 표시 규칙 — 배포됐거나 기록 비용 > 0 일 때만 앱별 웹 검색 칩. 아니면 종전과 같이 비운다.
  const showWs = webSearchVisible(data.web_search);
  const [hoverIndex, setHoverIndex] = useState<number | null>(null);
  const otherLabel = t('clientOther');
  const pieData = useMemo(
    () =>
      data.clients.map((c, i) => ({
        name: labelFor(c.client, otherLabel),
        value: c.cost_usd,
        fill: COLORS[i % COLORS.length],
      })),
    [data, otherLabel],
  );
  // 각자 toFixed(1) 하면 99.95% + 0.05% 가 100.0% + 0.1% = 100.1% 로 보인다 —
  // 최대 나머지 방식으로 합을 100.0 에 맞추고, 0 으로 내려간 비0 항목은 '<0.1%'.
  const sharesRounded = useMemo(
    () => roundSharesTo100(data.clients.map((c) => c.share_pct)),
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
        <ShareDonut data={pieData} onHover={setHoverIndex} />
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
                {centerItem ? labelFor(centerItem.client, otherLabel) : ''}
              </p>
              <p className="mt-0.5 text-[10px] text-muted-foreground tabular-nums">
                {hoverIndex != null && centerItem
                  ? <Usd value={centerItem.cost_usd} />
                  : <span title={usdTitle(data.total_cost_usd)}>
                      {t('topShare', { total: fmtUsd(data.total_cost_usd) })}
                    </span>}
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
            className={`grid grid-cols-[minmax(0,1fr)_5rem_minmax(6.5rem,auto)_5rem_3.5rem] items-baseline gap-2 text-sm rounded-apple-sm px-2 py-0.5 -mx-2 transition-colors ${i === hoverIndex ? 'bg-accent' : ''}`}
          >
            <div className="flex items-start gap-2 min-w-0">
              <span
                className="w-3 h-3 rounded-full flex-shrink-0 mt-1"
                style={{ backgroundColor: COLORS[i % COLORS.length] }}
              />
              {/* ModelShareDonut 과 동일 — 하이픈 토큰 중간 줄바꿈 방지(L5). */}
              <span className="font-medium break-words leading-snug" title={labelFor(c.client, otherLabel)}>
                {labelFor(c.client, otherLabel).split(' ').map((tok, ti) => (
                  <span key={ti} className={tok.includes('-') ? 'whitespace-nowrap' : undefined}>
                    {tok}{ti < labelFor(c.client, otherLabel).split(' ').length - 1 ? ' ' : ''}
                  </span>
                ))}
              </span>
            </div>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {t('callCount', { count: c.call_count })}
            </span>
            {/* 웹 검색 수 · 그 비용(모니터링 전용) — 오른쪽 비용·점유율은 모델 비용만이다. */}
            <span className="text-muted-foreground tabular-nums text-xs text-right whitespace-nowrap">
              {showWs && c.web_search_count > 0 && (
                <span data-testid="client-web-search">
                  {t('webSearchCount', { count: c.web_search_count })}
                  {' · '}
                  <WebSearchCostValue cost={c.web_search_cost_usd ?? null} />
                </span>
              )}
            </span>
            <span className="tabular-nums text-xs text-right"><Usd value={c.cost_usd} /></span>
            <span className="text-muted-foreground tabular-nums text-xs text-right">
              {fmtSharePct(sharesRounded[i], c.share_pct)}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
