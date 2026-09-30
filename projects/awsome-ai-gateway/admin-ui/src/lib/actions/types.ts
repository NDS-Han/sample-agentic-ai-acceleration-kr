// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * Common ActionResult type for all Server Actions.
 * T defaults to void for actions that don't return data.
 */
/** 409 confirmation_required 응답 — details.warnings[] 에 영향 목록이 실린다
 *  (budget-rules.md §3-0). UI 는 이를 보여주고 confirm=true 로 재요청한다. */
export interface ConfirmationPayload {
  message: string;
  details?: unknown;
}

export type ActionResult<T = void> =
  | { success: true; data: T }
  | {
      success: false;
      error: string;
      fieldErrors?: Record<string, string>;
      confirmation?: ConfirmationPayload;
    };
