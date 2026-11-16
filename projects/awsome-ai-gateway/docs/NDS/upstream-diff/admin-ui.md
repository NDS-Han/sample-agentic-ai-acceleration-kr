# admin-ui — upstream 대비 변경 전체

upstream 페이지: analytics·budgets·chat·cli·keys·models·monitoring·
my·rate-limits·users. NDS는 **신규 페이지 5종** + 전면 i18n + 정책
편집 UX 대규모 확장(+36,523 라인).

## 1. 신규 페이지

| 페이지 | 기능 |
|---|---|
| `/apps` | 앱 정책 콘솔 — per-app 기본 모델·모델 접근(allowed_clients) |
| `/audit` | 감사 로그 뷰어 |
| `/agents` | admin-chat-agent 관리·메시지 리포트 |
| `/integration` | 외부 앱 연동 샘플 — `/cli` 페이지 전환 + 연동 예제 |
| `/service-accounts` | 서비스 계정 패널 — 사용자 검색 등록·장기키·해제 플로우 |

## 2. 전면 i18n (ko/en)

모든 화면을 한/영 전환 구조로 재작성 — 인도 배포 코드 동기화
(`REPORTING_TIMEZONE` 설정화와 함께 upstream으로 올린 적 있음).

## 3. 정책·사용자 편집 UX

- **`/users`** — `?team`/`?user` 딥링크, 팀 행 다운그레이드 배지,
  정책 섹션 구조(상태 배지·통합 Apply·무제한 표현 통일),
  rate-limit 편집 패널 편입(`/rate-limits` 페이지 폐지),
  팀 유효 정책 카드, "팀 정책 따라가기"·트리 개별설정,
  자동 다운그레이드 임계값 UI + 사용량 시뮬레이터,
  다운그레이드 DAG 다이어그램 + 규칙 편집
- **CAP 모델** — D 표시/편집·균등분배·초과약정률·bulk 편집·검색·
  확인 흐름(D-7,21,22,23), 팀 기본 유저 cap 컬럼
- **키 발급** — 장기 키 발급 단일 모달 플로우 재설계, 계정 선택 시
  서비스 계정 자동 전환, 발급 비활성 시 이유 안내(버튼만 사라지는 함정 해소)

## 4. 모델 관리

- LiteLLM 스타일 확장 행 재구성 — 라우팅 필드 편집·테이블 배지,
  spec 필드(context_window/max_output), provider_model_id 노출,
  장문 티어 UI, 단가 입력 USD/1M 기준, 캐시 단가 기본값·1M 단위 명시,
  hard delete(deprecated 정리), Add Model에 alias 라우팅 의미 설명

## 5. 분석·모니터링

- 임의 기간(custom range) 분석, rate-limit scope별 과거 사용량 시계열
- 채팅 drain/크로스파드·대시보드 분모 정합, lazy trace,
  필터/페이지네이션·렌더 보완(관리자 UX 리뷰)
- BI Insight·web search 미배포 환경에서 UI 숨김/비활성화
- 인프라 미프로비저닝이면 런타임 토글 잠금

## 6. 인증 흐름

- 로그인 콜백: 토큰 검증 → **`/v1/auth/provision` 등록** → `/admin/my/profile`
  역할 조회 — 등록 실패 시 쿠키 미발급(fail-closed)
- 로그아웃 시 IdP 세션 종료(Cognito `/logout`) + 로컬 쿠키 정리
- DEVELOPER는 `/` 403 대신 `/my`로 보냄(로그인 직후 403 첫 화면 방지)
- 미들웨어 만료 처리를 실제 `isSessionExpired` 계약으로 정합

## 7. 공통 컴포넌트

- `NumberInput` — 휠 스크롤 값 변동 차단
- 정책 배지 로딩 스켈레톤(클릭 후 빈 헤더에 늦게 튀는 배지 개선)
