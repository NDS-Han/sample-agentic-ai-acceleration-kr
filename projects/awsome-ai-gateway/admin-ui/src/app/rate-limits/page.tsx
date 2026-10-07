// Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

// Rate limit 관리는 /users 의 유저·팀 패널(Rate limit 섹션)로 이전됐다.
// 사용량 프록시(/api/rate-limits/*)는 그대로 유지. PAGE_PERMISSIONS 의
// '/rate-limits' 항목도 유지 — middleware 가 이 redirect 보다 먼저
// default-deny 하므로 지우면 admin 도 /403 으로 간다.
import { redirect } from 'next/navigation';

export default function RateLimitsPage() {
  // ?from=rate-limits → /users 상단에 "rate limit 은 유저/팀 패널로 이동" 안내 배너.
  // 첫 노드 선택 시 ?node= 로 덮어씌워져 자연 소멸한다.
  redirect('/users?from=rate-limits');
}
