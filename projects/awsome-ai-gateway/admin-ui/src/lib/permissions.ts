// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

import type { PagePermissionMap } from '@/types/api';
import { UserRole } from '@/types/enums';

/**
 * Canonical page-permission table.
 *
 * Keys are exact pathname prefixes (matched with startsWith in auth.ts).
 * Values list the UserRole values that are ALLOWED to visit that path.
 *
 * DEVELOPER role is intentionally absent from all entries — developers are
 * redirected to a "no access" page by the middleware.
 */
export const PAGE_PERMISSIONS: PagePermissionMap = {
  // Dashboard
  '/': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // Virtual key management — admin only
  '/keys': [UserRole.ADMIN],

  // Budget overview — admin + team leader
  '/budgets': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // Model catalogue — admin only
  '/models': [UserRole.ADMIN],

  // Rate-limit configuration — admin only
  '/rate-limits': [UserRole.ADMIN],

  // User / org management — admin only
  '/users': [UserRole.ADMIN],

  // Analytics & ROI — admin + team leader
  '/analytics': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // Self-service usage — team leaders and developers only.
  // ADMIN 은 게이트웨이 전체를 관리하는 역할이므로 "내 사용량" 을 노출하면
  // 역할 경계가 흐려진다는 피드백에 따라 제외. 시스템 전체 사용량은
  // /monitoring, /analytics 가 담당.
  '/my': [UserRole.TEAM_LEADER, UserRole.DEVELOPER],

  // Real-time monitoring — admin only
  '/monitoring': [UserRole.ADMIN],

  // CLI downloads — admin + team leader
  '/cli': [UserRole.ADMIN, UserRole.TEAM_LEADER],

  // BI assistant chat — ADMIN 전용.
  // ⚠️ 예전 주석은 "/analytics 와 동일 범위(admin + team leader)" 였지만 백엔드와 어긋난다:
  //    admin-api/src/app/routers/chat_agent.py 의 8개 엔드포인트가 전부
  //    Depends(require_admin) 이다(:78, :122, :182, :211, :324, :385, :756, :841).
  //    TEAM_LEADER 를 허용하면 페이지는 열리는데 세션 생성·스트림·리포트 다운로드가 모두
  //    403 이라 아무것도 못 하는 화면이 된다. 넓히려면 백엔드 authz 를 먼저 바꿔야 하고,
  //    그때는 팀 범위 데이터 격리(SQL 이 다른 팀 사용량을 읽지 못하게)도 함께 설계해야 한다.
  '/chat': [UserRole.ADMIN],
};
