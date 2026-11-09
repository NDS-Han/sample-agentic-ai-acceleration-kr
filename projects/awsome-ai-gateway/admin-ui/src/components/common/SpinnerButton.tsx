'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.


import { Loader2 } from 'lucide-react';
import { buttonClass, type ButtonSize, type ButtonVariant } from './Button';

interface SpinnerButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  isLoading?: boolean;
  children: React.ReactNode;
  variant?: ButtonVariant;
  size?: ButtonSize;
}

/**
 * 로딩 스피너 버튼 — 스타일은 Button/buttonClass 에 위임한다.
 * (기존엔 primary 스타일 고정 + className 덮어쓰기라, Tailwind 는 생성 CSS 순서로
 * 승자가 정해져 bg-destructive 오버라이드가 깨질 수 있었다.)
 */
export function SpinnerButton({
  isLoading = false,
  children,
  disabled,
  className = '',
  variant = 'primary',
  size = 'md',
  ...props
}: SpinnerButtonProps) {
  const isDisabled = isLoading || disabled;

  return (
    <button
      {...props}
      disabled={isDisabled}
      aria-disabled={isDisabled}
      aria-busy={isLoading}
      className={buttonClass({ variant, size, className })}
    >
      {isLoading && (
        <Loader2
          size={16}
          className="animate-spin flex-shrink-0"
          aria-hidden="true"
        />
      )}
      {children}
    </button>
  );
}
