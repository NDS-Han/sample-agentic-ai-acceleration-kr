'use client';

// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import { forwardRef } from 'react';

/**
 * type="number" 입력 래퍼 — 포커스된 상태에서 마우스 휠/트랙패드 스크롤이
 * 값을 증감시키는 브라우저 기본 동작을 제거한다. 가격·한도·예산 입력에서
 * 폼을 스크롤하려다 값이 바뀌는 사고를 막기 위한 것.
 *
 * React 의 onWheel 은 루트 위임 + passive 리스너라 preventDefault 가 안 먹힌다.
 * blur() 가 실질적인 차단 수단이다(포커스가 빠지면 wheel 이벤트가 input 에
 * 도달하지 않는다). 나머지 속성은 네이티브 input 과 동일하게 전달된다.
 */
export const NumberInput = forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(function NumberInput({ onWheel, ...props }, ref) {
  return (
    <input
      {...props}
      ref={ref}
      type="number"
      onWheel={(e) => {
        e.currentTarget.blur();
        onWheel?.(e);
      }}
    />
  );
});
