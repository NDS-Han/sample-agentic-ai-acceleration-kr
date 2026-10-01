// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import Link from 'next/link';
import type { AlertLevel } from '@/types/enums';
import { AlertLevel as AlertLevelConst } from '@/types/enums';

interface KPICardProps {
  title: string;
  value: string | number;
  icon: React.ReactNode;
  alertLevel?: AlertLevel;
  /** 경보 심각도의 텍스트 표기 — 색만으로 전달되지 않도록(접근성).
   *  alertLevel 이 NORMAL 이 아닐 때 description 앞에 배지처럼 표시된다. */
  alertLabel?: string;
  description?: string;
  /** 주어지면 카드 전체가 관련 페이지로 가는 링크가 된다. */
  href?: string;
}

const ALERT_BORDER_CLASSES: Record<AlertLevel, string> = {
  [AlertLevelConst.CRITICAL]: 'border-destructive shadow-destructive/20',
  [AlertLevelConst.WARNING]: 'border-warning shadow-warning/20',
  [AlertLevelConst.NORMAL]: 'border-border',
};

const ALERT_ICON_WRAPPER_CLASSES: Record<AlertLevel, string> = {
  [AlertLevelConst.CRITICAL]: 'bg-destructive/10 text-destructive',
  [AlertLevelConst.WARNING]: 'bg-warning/10 text-warning',
  [AlertLevelConst.NORMAL]: 'bg-muted text-muted-foreground',
};

const ALERT_TEXT_CLASSES: Record<AlertLevel, string> = {
  [AlertLevelConst.CRITICAL]: 'text-destructive',
  [AlertLevelConst.WARNING]: 'text-warning',
  [AlertLevelConst.NORMAL]: 'text-muted-foreground',
};

export function KPICard({
  title,
  value,
  icon,
  alertLevel = AlertLevelConst.NORMAL,
  alertLabel,
  description,
  href,
}: KPICardProps) {
  const borderClass = ALERT_BORDER_CLASSES[alertLevel];
  const iconWrapperClass = ALERT_ICON_WRAPPER_CLASSES[alertLevel];

  const className = [
    'glass glass-hover rounded-apple p-6 flex flex-col gap-4',
    // normal 은 glass 기본 보더, alert 일 때만 강조 보더 덮어쓰기
    alertLevel === AlertLevelConst.NORMAL ? '' : borderClass,
    // 링크 모드: 키보드 포커스 링 — div 경로에는 불필요.
    href ? 'block focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring' : '',
  ].join(' ');
  const ariaLabel = `${title}: ${value}`;

  const inner = (
    <>
      {/* Header: title + icon */}
      <div className="flex items-center justify-between">
        <p className="text-sm font-medium text-muted-foreground">{title}</p>
        <div
          className={[
            'flex h-9 w-9 items-center justify-center rounded-md flex-shrink-0',
            iconWrapperClass,
          ].join(' ')}
          aria-hidden="true"
        >
          {icon}
        </div>
      </div>

      {/* Value */}
      <p className="text-3xl font-bold text-foreground tracking-tight">
        {typeof value === 'number' ? value.toLocaleString() : value}
      </p>

      {/* Description — 경보 심각도는 색뿐 아니라 텍스트로도 전달. */}
      {(alertLabel || description) && (
        <p className="text-xs text-muted-foreground">
          {alertLabel && alertLevel !== AlertLevelConst.NORMAL && (
            <span className={`font-semibold ${ALERT_TEXT_CLASSES[alertLevel]}`}>
              {alertLabel} ·{' '}
            </span>
          )}
          {description}
        </p>
      )}
    </>
  );

  // 링크 모드에서는 aria-label 을 두지 않는다 — 자식 텍스트(제목·값·설명)가
  // 접근성명이 되어 스크린리더가 카드 내용을 그대로 읽는다. aria-label 로
  // 덮으면 "제목: 값" 만 읽혀 링크 목적지 단서가 사라진다.
  return href ? (
    <Link href={href} className={className}>
      {inner}
    </Link>
  ) : (
    <div className={className} aria-label={ariaLabel}>
      {inner}
    </div>
  );
}
