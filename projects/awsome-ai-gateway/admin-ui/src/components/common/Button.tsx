'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { ButtonHTMLAttributes } from 'react';

export type ButtonSize = 'sm' | 'md';
export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'link' | 'danger' | 'dangerOutline';

const SIZES: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-xs',
  md: 'px-4 py-2 text-sm',
};

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'rounded-md bg-primary font-medium text-primary-foreground shadow hover:bg-primary/90 disabled:opacity-50',
  secondary:
    'rounded-md bg-secondary font-medium text-secondary-foreground hover:bg-secondary/80 disabled:opacity-50',
  ghost:
    'rounded-md border border-border bg-background font-medium hover:bg-muted disabled:opacity-50',
  link: 'text-primary hover:underline underline-offset-2 disabled:opacity-50',
  // 파괴적 확인(다이얼로그의 삭제/폐기 확정) — solid.
  danger:
    'rounded-md bg-destructive font-medium text-destructive-foreground shadow hover:bg-destructive/90 disabled:opacity-50',
  // 파괴적 액션 진입(행 버튼) — 농도 3종(/30+/10, /40+/5+/15, /50+solid hover)이 갈렸던 것을 한 스펙으로.
  dangerOutline:
    'rounded-md border border-destructive/40 bg-background font-medium text-destructive hover:bg-destructive/10 disabled:opacity-50',
};

/** <button> 못 쓰는 <a> 다운로드 버튼 등은 클래스만 가져다 쓴다. */
export function buttonClass({
  size = 'md',
  variant = 'primary',
  className = '',
}: {
  size?: ButtonSize;
  variant?: ButtonVariant;
  className?: string;
} = {}): string {
  return [
    'inline-flex items-center justify-center gap-1.5 transition-colors',
    'focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring',
    SIZES[size],
    VARIANTS[variant],
    className,
  ].join(' ');
}

/** primary 버튼 크기를 한 곳에서 — 화면마다 px/py 가 달라지던 표류 방지. */
export function Button({
  size = 'md',
  variant = 'primary',
  className = '',
  type = 'button',
  ...props
}: ButtonHTMLAttributes<HTMLButtonElement> & {
  size?: ButtonSize;
  variant?: ButtonVariant;
}) {
  return (
    <button type={type} className={buttonClass({ size, variant, className })} {...props} />
  );
}
