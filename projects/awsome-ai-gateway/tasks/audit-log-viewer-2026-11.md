# 감사 로그 뷰어 (2026-11)

## 목적
audit.audit_logs(기록 인프라 완비, ~60 action)의 조회 API + 관리 UI 부재 → 읽기 전용 뷰어를 만든다.

## 설계 제약 (kiro opus 5.5 분석에서 확정)
- changes는 {before,after} 비통일 — 5가지 모양(after만/배열/스칼라/평평dict/_unserializable) → classifyChanges 분류 함수
- FAILURE는 VIEW_INVOCATION_LOG_BODY만 남음(세션 롤백과 함께 사라짐) → 결과 필터 제공 안 함
- 합성 행위자 2개: …000010=dev, …000011=서비스 토큰 공용 → changes._service_token_id 배치 해소
- budget 액션 동적: *_CONFIRMED 접미사 → 그룹 매핑은 정규화 후
- (timestamp DESC) 전체 스캔 인덱스 없음 → 0053 마이그레이션 필요. CONCURRENTLY 불가(env.py txn)
- /admin/audit prefix는 audit_reconcile 점유 → 같은 라우터에 /logs, /action-catalog 추가
  (⚠️ {bedrock_request_id} 보다 먼저 선언 — 리터럴 경로 순서가 계약)

## 범위
1. 0053: idx_audit_logs_ts_id (timestamp DESC, id DESC)
2. core/audit_catalog.py: ACTION_GROUPS 6그룹+other, normalize(_CONFIRMED), group_for_action
3. GET /admin/audit/logs — range(24h|7d|30d,필수)/group/action/actor_email/resource_type/resource_id/cursor/limit, keyset(timestamp,id), 행위자 이메일·서비스토큰 배치 해소(페이지당 ≤2쿼리)
4. GET /admin/audit/action-catalog — 그룹→정규화 액션 목록
5. UI /audit (ADMIN): 서버 컴포넌트(필터=Link/form, 전부 URL 쿼리) + AuditLogListView(더보기, key=필터조합 remount) + ChangesPanel + ActionSelect
6. 사이드바 ScrollText /audit + PAGE_PERMISSIONS + i18n audit.*

## 제외
결과 필터 / 컬럼 정렬 / changes 자유검색 / TEAM_LEADER 접근 / CSV / resource_id 이름 해소 / 보존 정책
