// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// 모델별 상세 비용은 /analytics 페이지의 하단 섹션으로 통합됐다 — 별도 페이지가
// 두 개일 이유가 사라져 탭/뒤로가기 네비게이션 자체를 제거. 쿼리 파라미터
// (period·start_date·end_date·group_by·scope)는 그대로 넘겨 필터 상태가 보존된다.
import { redirect } from 'next/navigation';

interface ModelCostPageProps {
  searchParams: Record<string, string | string[] | undefined>;
}

export default function ModelCostPage({ searchParams }: ModelCostPageProps) {
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(searchParams)) {
    if (typeof v === 'string') qs.set(k, v);
  }
  const suffix = qs.toString();
  redirect(`/analytics${suffix ? `?${suffix}` : ''}`);
}
