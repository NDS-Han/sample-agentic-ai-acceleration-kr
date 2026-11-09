'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 세그먼트 컨트롤 — 탭형 필터의 단일 스타일.
 *
 * 표류 배경: 대시보드는 glass 그룹 + soft tint, keys/audit 은 개별 pill +
 * solid primary(프라이머리 버튼처럼 보임) 두 계통이 6벌 복사됐다. 활성 표시를
 * 'solid 채움'이 아니라 '틴트 + inset 보더' 한 계통으로 통일한다 — solid 는
 * 실행 버튼과 시각이 겹쳐 "누르면 동작"으로 오독된다.
 *
 * 사용:
 *   상태/라우팅은 호출자 — <Segmented>(버튼형, 클라이언트 state/router.push),
 *   <SegmentedLinks>(링크형, 서버 페이지 ?key=value 네비), 직접 조합은
 *   <SegmentedGroup> + segmentedItemClass().
 */

import Link from 'next/link';
import type { ReactNode } from 'react';
import { cn } from '@/lib/utils/cn';

/** 세그먼트 아이템 공통 클래스 — 활성 = primary 틴트 + inset 링. */
export function segmentedItemClass(active: boolean): string {
  return [
    'pressable rounded-apple-sm px-3 py-1.5 text-sm font-medium transition-[background,color,box-shadow] duration-150',
    active
      ? 'bg-primary/10 text-primary font-semibold shadow-[inset_0_0_0_1px_hsl(var(--primary)/0.18)]'
      : 'text-muted-foreground interactive',
  ].join(' ');
}

export function SegmentedGroup({
  label,
  children,
  className = '',
}: {
  label: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className={cn('glass inline-flex items-center gap-0.5 rounded-apple-md p-1', className)}
    >
      {children}
    </div>
  );
}

interface SegmentedItem {
  value: string;
  label: ReactNode;
}

/** 버튼형 — 선택 상태는 호출자 소유(router.push·setState 등). */
export function Segmented({
  label,
  items,
  value,
  onSelect,
  className = '',
}: {
  label: string;
  items: SegmentedItem[];
  value: string;
  onSelect: (_value: string) => void;
  className?: string;
}) {
  return (
    <SegmentedGroup label={label} className={className}>
      {items.map((it) => (
        <button
          key={it.value}
          type="button"
          onClick={() => onSelect(it.value)}
          aria-pressed={value === it.value}
          className={segmentedItemClass(value === it.value)}
        >
          {it.label}
        </button>
      ))}
    </SegmentedGroup>
  );
}

interface SegmentedLinkItem extends SegmentedItem {
  href: string;
  current?: boolean;
}

/** 링크형 — 서버 컴포넌트 필터 탭(?status=active)용. */
export function SegmentedLinks({
  label,
  items,
  className = '',
}: {
  label: string;
  items: SegmentedLinkItem[];
  className?: string;
}) {
  return (
    <SegmentedGroup label={label} className={className}>
      {items.map((it) => (
        <Link
          key={it.value}
          href={it.href}
          aria-current={it.current ? 'page' : undefined}
          className={segmentedItemClass(it.current === true)}
        >
          {it.label}
        </Link>
      ))}
    </SegmentedGroup>
  );
}
