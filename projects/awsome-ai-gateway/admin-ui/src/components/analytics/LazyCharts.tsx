'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import dynamic from 'next/dynamic';
import type { TokenBreakdownData } from './TokenMixDonutClient';

// ⚠️ ssr:false 는 Server Component 안의 next/dynamic 에서 쓰면
// BAILOUT_TO_CLIENT_SIDE_RENDERING 경계가 생겨 hydration 타이밍에 따라
// 차트가 영구히 빈 상태로 남는다(비용 추이가 간헐적으로 안 보이던 원인).
// ssr:false 는 Client Component 에서만 허용되므로, 여기서 감싼다.
// 첫 진입 시 번들 fetch 동안 카드가 통째로 비어 보이던 것(UX 리뷰 L6) —
// 차트 형태의 플레이스홀더로 로딩 중임을 보인다.
function ChartSkeleton() {
  return (
    <div className="h-64 animate-pulse rounded-apple bg-muted/50" aria-hidden="true" />
  );
}

const BreakdownChartClient = dynamic(
  () =>
    import('./BreakdownChartClient').then((mod) => ({ default: mod.BreakdownChartClient })),
  { ssr: false, loading: ChartSkeleton }
);

const TokenMixDonutClient = dynamic(
  () =>
    import('./TokenMixDonutClient').then((mod) => ({ default: mod.TokenMixDonutClient })),
  { ssr: false, loading: ChartSkeleton }
);

export function LazyBreakdownChart({
  labels,
  values,
  title,
}: {
  labels: string[];
  values: number[];
  title: string;
}) {
  return <BreakdownChartClient labels={labels} values={values} title={title} />;
}

export function LazyTokenMixDonut({ data }: { data: TokenBreakdownData }) {
  return <TokenMixDonutClient data={data} />;
}
