'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { forwardRef } from 'react';
import { cn } from '@/lib/utils/cn';

/**
 * type="number" 입력 래퍼 — 숫자 입력에서 사고를 내는 브라우저 기본 동작 2가지를 제거한다.
 *
 * 1. 휠/트랙패드 스크롤로 값이 증감 — React 의 onWheel 은 루트 위임 + passive
 *    리스너라 preventDefault 가 안 먹혀 blur() 로 차단한다(포커스가 빠지면
 *    wheel 이벤트가 input 에 도달하지 않는다).
 * 2. 위아래 스피너 화살표 — 클릭/터치로 값이 바뀌는 또 다른 경로이고, 가격 같은
 *    소수 단가에는 스텝 스피너가 실용적이지 않다. appearance/textfield 계열
 *    CSS 로 숨긴다(Webkit + Firefox 커버).
 *
 * 나머지 속성은 네이티브 input 과 동일하게 전달된다.
 */
export const NumberInput = forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(function NumberInput({ onWheel, className, ...props }, ref) {
  return (
    <input
      {...props}
      ref={ref}
      type="number"
      className={cn(
        // Firefox: textfield 모드, Webkit: inner/outer 스핀버튼 숨김.
        '[appearance:textfield] [&::-webkit-outer-spin-button]:appearance-none [&::-webkit-inner-spin-button]:appearance-none',
        className
      )}
      onWheel={(e) => {
        e.currentTarget.blur();
        onWheel?.(e);
      }}
    />
  );
});
