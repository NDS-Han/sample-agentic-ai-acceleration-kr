'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { ButtonHTMLAttributes } from 'react';

export type ButtonSize = 'sm' | 'md';
export type ButtonVariant = 'primary' | 'ghost' | 'link';

const SIZES: Record<ButtonSize, string> = {
  sm: 'px-3 py-1.5 text-xs',
  md: 'px-4 py-2 text-sm',
};

const VARIANTS: Record<ButtonVariant, string> = {
  primary:
    'rounded-md bg-primary font-medium text-primary-foreground shadow hover:bg-primary/90 disabled:opacity-50',
  ghost:
    'rounded-md border border-border bg-background font-medium hover:bg-muted disabled:opacity-50',
  link: 'text-primary hover:underline underline-offset-2 disabled:opacity-50',
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
