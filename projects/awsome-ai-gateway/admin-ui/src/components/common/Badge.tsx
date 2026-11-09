// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { ReactNode } from 'react';

export type BadgeTone = 'teal' | 'sky' | 'pink' | 'amber' | 'neutral' | 'danger';

interface BadgeProps {
  tone?: BadgeTone;
  children: ReactNode;
  className?: string;
}

const TONE_CLASS: Record<BadgeTone, string> = {
  teal: 'badge-teal',
  sky: 'badge-sky',
  pink: 'badge-pink',
  amber: 'badge-amber',
  neutral: 'badge-neutral',
  danger: 'badge-danger',
};

/**
 * 상태/역할/프로바이더 등 라벨 배지. teal/sky/pink/amber/neutral/danger 톤을
 * globals.css 의 그라데이션 유틸로 통일.
 *
 * 의미 규약: danger=오류·실패·차단(파괴), amber=경고·주의, pink=ADMIN 역할 전용,
 * neutral=비활성·폐기 등 상태 없음. 상태 배지에 역할색(pink)을 섞지 않는다.
 */
export function Badge({ tone = 'neutral', children, className = '' }: BadgeProps) {
  return <span className={`badge ${TONE_CLASS[tone]} ${className}`}>{children}</span>;
}

/** 계정 역할 배지 공통 톤 — 헤더·예산·사용자·감사가 같은 색을 쓴다. */
export const ROLE_TONE: Record<string, BadgeTone> = {
  ADMIN: 'pink',
  TEAM_LEADER: 'sky',
  DEVELOPER: 'neutral',
};

/**
 * 역할 배지 — label 은 호출자가 번역해 넘긴다(t('auth.role.X') 등).
 * 알 수 없는 role 도 neutral 로 표시해 생략하지 않는다.
 */
export function RoleBadge({ role, label, className = '' }: { role?: string | null; label: string; className?: string }) {
  return <Badge tone={ROLE_TONE[role ?? ''] ?? 'neutral'} className={className}>{label}</Badge>;
}
