# R44 — R43 이후 신규 코드 델타 리뷰

날짜: 2026-10-26 · 범위: R43 종합 이후 커밋된 신규 코드.
대상: 모델 레벨 계정 라우팅(b38045c3·17cb9f80·bdf088ed·0517a4be·8d645e41),
eks apply --plan diff(ee2d1ef5), 클라이언트 프로필 라우팅 API+UI(ac836b9d),
DR 런북 정합성(ac836b9d). CI 워크플로우는 사용자 지시로 제외.

## 발견·수정

| # | 항목 | 심각도 | 조치 |
|---|---|---|---|
| F1 | **dr-runbook이 없는 terraform output `aurora_cluster_id`를 참조** — env outputs에는 `aurora_endpoint`/`rds_proxy_endpoint`만 있음. 리허설 시 그 자리에서 실패 | 문서 | 수정 — describe-db-clusters로 식별자 확인하는 절차로 교체 |
| F2 | `helm get manifest`는 hook을 포함 — plan diff가 migration job을 매번 "추가"로 표시할 우려 | 조사 | **거짓양성 없음 확인** — get manifest 출력에 hook 리소스가 포함되므로 정상 비교됨 |
| F3 | 라우팅 프로필 `list`/`GET app policy`가 external_id를 평문 반환 | 정보 | 허용 판정 — 모델 라우팅 응답과 동일 정책(ADMIN 전용, 감사 로그만 마스킹). 일관성 유지 |

## 검증된 부분 (갭 아님)

- **model_account_route/apply_model_account_route** — 프로필 복사본 반환(공유 캐시 오염 없음), ARN-only NULL-region 행은 라우팅 미지정+ loud error 로그(fail-open이 아니라 프로필 폴백=0049 이전 동작, 의도된 절충), 어노테이션으로 직접-SQL 상태도 방어
- **fallback 후보 격리** — 후보마다 어댑터/프로필/리전 재해석, (role_arn, external_id) 단위 캐시 분리
- **passthrough fail-closed** — 모델 ARN + cross-account client 부재 시 503 (silent in-account 폴백 없음)
- **eks plan diff** — NOTES 섹션 절단, all-caps 섹션 마커 필터, 정규화 YAML 비교, live unreadable 시 전체 덤프 폴백 — 경계 처리 정직
- **client routing PUT** — 모델 라우팅 검증기 재사용(규칙 단일 출처), commit→invalidate→audit 순서, eid 마스킹, UI dirty 가드 + 로드 드레인에 routing 카운터 포함

## 잔여 (선택적 후속)

- `routing_profiles.backend`/`enabled`는 여전히 SQL 전용 — 의도된 한계(UI는 앱 스위치가 아니라 크로스계정 라우팅만 편집)
- 모델 라우팅에 `region` NULL + ARN 행(직접 SQL)은 프로필 폴백으로 읽힘 — fail-closed 아님. 직접-SQL 운영 시 주의사항으로 문서화 수준에서 충분
- G2 UI의 clear는 ARN을 비워 저장하는 방식 — 별도 "해제" 버튼 없음 (폼 빈칸 저장이 해제). 문서 힌트로 충분

## 다음 라운드 후보

- R45: `.github/workflows/awsome-ai-gateway-ci.yml` — 사용자 제외로 보류 중
- UX 잔여: L3(by_user 팀 병기 — 현상 유지 결정), M6 차트 통합
