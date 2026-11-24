'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/** /users 상세의 작은 조각 — 사용량 카드, 출처 배지, 정책 요약 줄. */

import Link from 'next/link';
import { useTranslations } from 'next-intl';
import { ChevronRight } from 'lucide-react';

import { UsageBar } from '@/components/budgets/budgetVisuals';
import { alertLevelOf } from '@/lib/utils/alertLevel';
import type { PolicyScope, PolicySource } from './policyRows';

export function KpiCard({
  label,
  value,
  sub,
  barPct,
  testId,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  /** 있으면 값 아래 사용률 막대. */
  barPct?: number | null;
  testId?: string;
}) {
  return (
    <div className="glass flex min-w-0 flex-col gap-1 rounded-apple p-4" data-testid={testId}>
      <span className="text-xs font-medium text-muted-foreground">{label}</span>
      <span className="truncate text-xl font-bold tabular-nums">{value}</span>
      {barPct != null && (
        <span className="mt-0.5">
          <UsageBar pct={Math.min(barPct, 100)} level={alertLevelOf(barPct)} />
        </span>
      )}
      {sub && <span className="text-[11.5px] text-muted-foreground">{sub}</span>}
    </div>
  );
}

export function KpiSkeleton({ n = 5 }: { n?: number }) {
  return (
    <>
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="h-[92px] animate-pulse rounded-apple bg-muted/60" aria-hidden="true" />
      ))}
    </>
  );
}

/** 출처 배지 — 보는 대상 기준(팀 뷰의 own = '팀 지정', 사용자 뷰의 own = '개별 지정'). */
export function SourceBadge({ source, scope }: { source: PolicySource; scope: PolicyScope }) {
  const t = useTranslations('usersDetail.source');
  const key =
    source === 'own' ? (scope === 'team' ? 'teamOwn' : 'userOwn') : source === 'team' ? 'inheritedTeam' : source === 'org' ? 'org' : 'none';
  return (
    <span
      className={`badge whitespace-nowrap ${source === 'own' ? 'badge-teal' : 'badge-neutral'}`}
      data-source={source}
    >
      {t(key)}
    </span>
  );
}

/** 정책 요약 한 줄 — 항목 · 값(+보조) · 출처 · [변경] 또는 다른 화면 링크. */
export function PolicyRow({
  label,
  value,
  sub,
  source,
  scope,
  action,
  testId,
}: {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  source: PolicySource | null;
  scope: PolicyScope;
  action: { kind: 'button'; label: string; onClick: () => void; disabled?: boolean } | { kind: 'link'; label: string; href: string };
  testId?: string;
}) {
  return (
    <div
      className="grid grid-cols-1 items-center gap-x-3 gap-y-1 border-t border-border py-3 first:border-t-0 sm:grid-cols-[8.5rem_minmax(0,1fr)_auto_auto]"
      data-testid={testId}
    >
      <span className="text-sm font-semibold">{label}</span>
      <span className="min-w-0">
        <span className="block text-sm">{value}</span>
        {sub && <span className="block text-xs text-muted-foreground">{sub}</span>}
      </span>
      <span>{source && <SourceBadge source={source} scope={scope} />}</span>
      <span className="sm:text-right">
        {action.kind === 'button' ? (
          <button
            type="button"
            onClick={action.onClick}
            disabled={action.disabled}
            className="inline-flex items-center justify-center whitespace-nowrap rounded-md border border-border bg-background px-3 py-1.5 text-xs font-medium hover:bg-accent transition-colors focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:opacity-50"
          >
            {action.label}
          </button>
        ) : (
          <Link
            href={action.href}
            className="inline-flex items-center gap-0.5 whitespace-nowrap rounded-md px-2 py-1.5 text-xs font-medium text-primary hover:underline focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            {action.label}
            <ChevronRight size={12} aria-hidden="true" />
          </Link>
        )}
      </span>
    </div>
  );
}

export function Section({
  title,
  aside,
  children,
  testId,
}: {
  title: React.ReactNode;
  aside?: React.ReactNode;
  children: React.ReactNode;
  testId?: string;
}) {
  return (
    <section className="glass rounded-apple p-5" data-testid={testId}>
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-[15px] font-semibold">{title}</h3>
        {aside && <span className="text-xs text-muted-foreground">{aside}</span>}
      </div>
      {children}
    </section>
  );
}
