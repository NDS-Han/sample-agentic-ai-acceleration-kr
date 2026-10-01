// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 정책 섹션 헤더 배지용 요약 — 편집 패널이 저장된 상태만 보고한다.
 * dirty(수정됨)는 별도 onDirtyChange 가 보고하므로 여기엔 없다.
 */
export interface PolicySummary {
  /** 정책 데이터 로드 완료 여부 — false 면 헤더 배지를 숨긴다. */
  loaded: boolean;
  /** 저장된 정책이 제한 목록인지 (false = 제한 없음/전체 허용). */
  restricted: boolean;
  /** 제한 목록 길이 — restricted 일 때만 의미 있다. */
  count: number;
  /** 정책 출처 — own=이 스코프 직접 설정, team=상위 상속, none=정책 없음. */
  source?: 'own' | 'team' | 'none';
}

export const EMPTY_POLICY_SUMMARY: PolicySummary = {
  loaded: false,
  restricted: false,
  count: 0,
};
