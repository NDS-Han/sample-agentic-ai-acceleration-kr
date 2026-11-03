# 키별 사용량 분석 + `/apps` 페이지 — 계획 (SWE-2 × kiro opus 5.5)

목표: 서비스 VK(에이전트용 장기키) 단위로 사용량/토큰/비용을 귀속·분석하고,
전용 `/apps` 화면에서 계정 목록 + 상세 분석을 제공한다.

## 결정 사항 (리뷰 합의)

- **식별자 = `key_prefix`** (`raw_vk[:11]`). gateway 가 Authorization 헤더에서
  직접 계산 — Redis auth 계약 변경 0. `virtual_keys.key_prefix`로 조인.
  `key_id`/`key_hash` 대안은 auth-cache 작성자 3곳 일관 변경 + 신규 컬럼이
  필요해 기각.
- **백필 없음** — 구 행은 NULL, API 응답에 `attribution_since` 명시.
- **인덱스 없이 시작** — 키별 조회는 `user_id+requested_at` 범위 인덱스 안에서
  prefix 필터. 필요해지면 별도 마이그레이션.
- **배포 순서**: 마이그레이션 → 워커 → 게이트웨이 → admin-api → admin-ui.
  (워커 INSERT 가 컬럼명 명시 → 순서 어기면 배치 실패)
- **App Policy의 app(클라이언트 분류)와 분리** — `app_slug` 확장은 후속.

## 단계

1. [ ] migration `0051`: `usage.usage_logs.key_prefix VARCHAR(16) NULL`
   + admin-api `UsageLog` 모델 미러 + init SQL 동기화
2. [ ] cost-recorder-worker: `CostStreamEntry.key_prefix` + INSERT 컬럼
   (payload 호환 — 구 엔트리는 None)
3. [ ] gateway-proxy: VK auth 경로에서 `key_prefix = raw[:11]` 계산 →
   entry 전달 (JWT 경로는 None). 원문 토큰 미유입 테스트
4. [ ] admin-api: `list_service_accounts` 월간 집계(GROUP BY 1회, N+1 해소)
   + `GET /admin/service-accounts/{user_id}/usage` (summary/by_model/daily,
   키 수명 구간 clamp, 30s 캐시, §59 SUCCESS+KST)
   + `list_keys` last_request_at 을 `(user_id, key_prefix)` 그룹으로
5. [ ] admin-ui: `/apps` 목록(패널 이전 + 월간 열) + `/apps/{id}` 상세
   (키 카드, KPI 4, 모델별 표, 일별 추이) + `/keys` 에 안내 바 +
   사이드바 `앱` 항목 + `apps` i18n 네임스페이스
6. [ ] 테스트: 워커 payload 호환, gateway prefix 추출(JWT=None),
   usage 엔드포인트 집계 정확성
7. [ ] 순서대로 배포 + 앱 호출 1건으로 E2E 검증(usage_logs.key_prefix 채워짐,
   /apps 화면 표시)

## 리뷰 체크포인트

- R1: 단계 1-3 완료 후 (스키마/파이프라인)
- R2: 단계 4 완료 후 (API)
- R3: 단계 5-6 완료 후 (UI) — 각 단계 kiro opus 5.5 리뷰
