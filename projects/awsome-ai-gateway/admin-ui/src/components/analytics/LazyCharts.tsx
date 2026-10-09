'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import dynamic from 'next/dynamic';
import type { TokenBreakdownData } from './TokenMixDonutClient';

// ⚠️ ssr:false 는 Server Component 안의 next/dynamic 에서 쓰면
// BAILOUT_TO_CLIENT_SIDE_RENDERING 경계가 생겨 hydration 타이밍에 따라
// 차트가 영구히 빈 상태로 남는다(비용 추이가 간헐적으로 안 보이던 원인).
// ssr:false 는 Client Component 에서만 허용되므로, 여기서 감싼다.
const BreakdownChartClient = dynamic(
  () =>
    import('./BreakdownChartClient').then((mod) => ({ default: mod.BreakdownChartClient })),
  { ssr: false }
);

const TokenMixDonutClient = dynamic(
  () =>
    import('./TokenMixDonutClient').then((mod) => ({ default: mod.TokenMixDonutClient })),
  { ssr: false }
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
