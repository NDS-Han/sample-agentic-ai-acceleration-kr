// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * audit 액션 이름 헬퍼 — `*_CONFIRMED` 변형과 원본을 같은 개념으로 다룬다.
 *
 * 서버는 확인-재실행을 `<base>_CONFIRMED` 행으로 기록하고, UI 라벨/칩 매칭은
 * 항상 정규화된 base 이름으로 한다. 접미사 규칙이 흩어지면 한쪽만 고쳐져
 * 라벨이 깨지므로 여기서만 정의한다.
 */
const CONFIRMED_SUFFIX = '_CONFIRMED';

export const isConfirmedAction = (action: string): boolean =>
  action.endsWith(CONFIRMED_SUFFIX);

export const baseAction = (action: string): string =>
  isConfirmedAction(action)
    ? action.slice(0, -CONFIRMED_SUFFIX.length)
    : action;
