// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

/**
 * 웹 검색 표시 규칙(대시보드·Analytics·모니터링·내 사용량·서비스 계정·Body Logs 공통).
 *
 * 보인다 = features.web_search 가 배포돼 있거나(deployed) 그 기간에 **기록된** 웹 검색
 * 비용이 0 보다 크다. 둘 다 아니면 웹 검색 UI 는 숨기고 화면은 이 기능 이전과 똑같이 그린다.
 * 배포는 안 됐는데 비용이 있으면 보이되 "현재 미배포" 배지를 단다(notDeployedBadge).
 */
export function webSearchVisible(
  ws: { deployed?: boolean; cost_usd?: number | null } | null | undefined,
): boolean {
  if (!ws) return false;
  return ws.deployed === true || (ws.cost_usd ?? 0) > 0;
}

/** 보이지만 배포는 안 된 상태 — "Web search is not deployed now" 배지. */
export function webSearchNotDeployed(
  ws: { deployed?: boolean; cost_usd?: number | null } | null | undefined,
): boolean {
  return webSearchVisible(ws) && ws?.deployed !== true;
}
