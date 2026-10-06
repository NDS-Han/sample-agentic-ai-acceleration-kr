# Admin UI 페이지별 UX 통합 리뷰

- 1차: Devin (스크린샷 + 소스 직접 검토)
- 2차: kiro-cli `claude-opus-5.5` (독립 리뷰, `/tmp/ux-review/opus-report.txt`)
- 스크린샷: ADMIN 계정, 1440px, 라이트모드, ko 로케일
- 양쪽이 동시에 지적한 항목은 ◆ 표시 (신뢰도 높음)
- `[✓검증]` = 소스 코드로 확인 완료 / `[미검증]` = 추가 확인 필요

---

## 🔴 TOP 우선순위 (양쪽 합의 + 검증)

### 1. Chart.js 한글 깨짐 ◆ [✓검증]
모든 canvas 차트(`/analytics`, `/analytics/models`, `/` 대시보드 차트)의 제목·축·범례가 `□□□`로 렌더. Chart.js는 `next/font`의 Pretendard를 상속받지 못하고 자체 폰트 스택(Helvetica/Arial)으로 canvas를 그림 → Hangul 글리프 없음.
**수정**: `ChartJS.register` 호출부에 `Chart.defaults.font.family = 'Pretendard Variable, Pretendard, system-ui, sans-serif'` 추가.
**파일**: `CostTrendChartClient.tsx`, `BreakdownChartClient.tsx`, `TokenMixDonutClient.tsx`, dashboard의 `*DonutClient.tsx`, `CostTrendCard.tsx`

### 2. `/users` 진입 시 데드엔드 ◆ [✓검증]
루트 노드("Default Organization")가 접힌 상태 + 우측 패널 전체가 "트리에서 항목을 선택하세요" 빈 공간. `/rate-limits`가 여기로 redirect되는데 rate limit이 어디 있는지 표시도 없음.
**수정**: 루트 자동 확장 + 첫 노드 자동 선택. `?node=` deep link 지원. `/rate-limits` redirect는 `/users#rate-limit` 또는 한 번 "Rate limit은 사용자/팀 관리로 이동했습니다" 토스트.

### 3. `/apps` "전체 허용" 컬럼 라벨 불일치 ◆ [✓검증]
`colAllowAll` = `전체 허용`인데 실제 셀은 앱별 on/off 칩 3개(CC/Cowork/Codex). "전체 허용 여부"가 아니라 "앱별 커버리지"를 보여줌. NULL(무제한)과 명시적 3개-전체가 시각적으로 구분 안 됨 — /models에서 겪은 멱등성 혼란과 동일 패턴.
**수정**: 헤더 → `앱별 허용 현황`. 칩 범례(켜짐/꺼짐=취소선) 추가. `CC` 약어 → `Claude Code` 또는 툴팁. NULL 행에 "향후 앱 포함" 표시.

### 4. `/cli` 환경변수가 placeholder ◆ [✓검증]
`<POOL_ID>`, `<COGNITO_APP_CLIENT_ID>`, `<gateway-host>` — 관리자 콘솔이 실제 값을 알고 있는데 수동 기입 요구. 문구는 "3개 값"인데 export는 5줄.
**수정**: 서버 컴포넌트에서 실제 값 주입 + 각 코드블록에 복사 버튼. (Opus 추가: 다운로드 버튼이 above-the-fold에 없음, Windows 블록에 env-var 단계 누락, Claude Code만 언급 → Cowork/Codex 설정도)

### 5. 비용/토큰 수치가 페이지 간 불일치 ◆ [✓검증]
- 대시보드 "총 토큰 수 317.3K (입력+출력+캐시 합계)" vs `/analytics/models` 표는 input+output만 (~6K) — **캐시 토큰이 ~98%인데 표에 컬럼 없음** → 합계가 안 맞아 보임
- "활성 모델 수": 대시보드/모니터링 = 카탈로그 22개 vs `/analytics/models` = 트래픽 있던 모델 3개 — 같은 라벨 다른 의미
- "활성 API Keys 0" 옆에 "총 요청 수 14" — VK 없이 요청이 어떻게 존재하는지 설명 없음
- `/analytics/models` `$/1M 토큰` 컬럼: Opus는 "×1000 오류"로 의심했으나 검증 결과 `cost_per_1k_tokens`(실효 단가, 캐시 토큰 포함 분모)를 `fmtPricePerM`으로 올바르게 변환 중. 진짜 문제는 **분모가 불명**(캐시 포함인지 아닌지) + 캐시 토큰 컬럼 부재로 검산 불가.
**수정**: 라벨 분리("카탈로그 모델 수" vs "사용된 모델 수"), 캐시 토큰 컬럼 추가, `$/1M 토큰` → `평균 실효 단가 ($/1M, 캐시 포함)`, "총 요청 수" → "성공 요청 수" + 에러 KPI 추가.

### 6. `/budgets` $0.00 의미 모호 (Opus 신규)
SSIR_Developers: 최대 $0.00 / 남은 $0.00 / 0.0% / 상태 **정상** — "미설정"인지 "무제한"인지 "차단"인지 불분명. "정상" 배지는 오해 유발.
**수정**: $0.00 → `미설정`/`무제한` 표시, 상태 라벨 분리, 예산 기간(월간? 리셋일?) 표시.

### 7. `/monitoring` 런타임 에러 + 빈 상태 스택 ◆
좌하단 "2 errors" 배지 — dev 오버레이인지 실 런타임 에러인지 [미검증], prod 빌드에서 확인 필요. 빈 상태 카드 3장("모델 트래픽 없음" / "사용자 트래픽 없음" / "이벤트 없음")이 연속으로 큰 공간 차지.
**수정**: prod 빌드로 에러 확인. 빈 상태 통합 또는 컴팩트화 + `/analytics` 링크 등 다음 행동 안내. 요약(1시간)과 로그(24시간)의 시간 창 불일치 라벨링.

---

## 페이지별 상세

### `/` 대시보드
- **[MED]** ◆ 도넛 범례 이름 3~4줄 wrap ("Claude / Sonnet / 5") → 범례 폭 확보 또는 말줄임+툴팁
- **[MED]** (Opus) 클라이언트 필터의 "기타" 탭 — 무엇이 기타인지 설명 없음 → 툴팁 "다른 User-Agent/직접 API 호출"
- **[LOW]** (Opus) 데이터 포인트 2개뿐인 추이 차트가 직선으로 오해 유발 → 포인트/바 표시
- **[LOW]** (Opus) "일 평균 소비 $0.04 · 월말 예상 $1.34"에 "예산 대비 n%" 추가 고려

### `/users`
- **[HIGH]** 초기 빈 패널 (TOP #2)
- **[MED]** (Opus) 페이지 레벨에 "rate limit·앱 접근이 상세 패널에 있음" 안내 없음 → 부제목 추가
- **[MED]** (Opus) Cognito 동기화 설명 문구가 긴 한 줄 → 배너/툴팁으로 + 마지막 동기화 시각 표시
- **[LOW]** "빈 팀 표시" 체크박스가 트리와 반대편 끝에 위치 → 검색창 옆으로

### `/apps`
- **[HIGH]** "전체 허용" 컬럼 라벨 (TOP #3)
- **[MED]** ◆ "기본 모델"이 통계 카드 + 폼에 이중 표시 → 카드 제거 또는 "현재 저장값" 힌트로
- **[MED]** (Opus) 저장 방식 혼재: 기본 모델은 저장 버튼, 모델 체크박스·웹서치는 즉시 저장 → 즉시 저장 섹션에 "변경 즉시 적용" 표기. (칭찬: NULL 해제/마지막 앱 해제 확인 다이얼로그는 잘 설계됨)
- **[MED]** (Opus) 허용 안 된 모델도 기본 모델로 설정 가능 → select를 allowed 모델로 필터
- **[MED]** (Opus) 22+ 모델 목록에 검색/전체선택 없음
- **[LOW]** "허용된 사용자 5" 카드 비클릭 → 하단 테이블로 앵커 링크

### `/budgets`
- **[HIGH]** $0.00 모호성 + 예산 기간 미표시 (TOP #6)
- **[MED]** (Opus) "비활성 팀/유저 포함" 기본 체크 → 기본 해제 또는 비활성 행 시각 구분
- **[MED]** "다운그레이드 3" 배지가 체인 내용을 안 보여줌 → 툴팁에 체인(Opus→Sonnet→Haiku)+임계치%
- **[LOW]** 사용률 바에 임계치 색/마커 (경고/위험 구간)
- **[LOW]** (Opus) 행 전체 클릭 가능하게, 미설정 행은 "설정 추가" 라벨

### `/keys`
- **[MED]** ◆ 빈 상태에 발급 방법 안내 없음 → "gateway-cli login으로 발급" + `/cli` 링크. "VK" 약어 설명 없음
- **[MED]** (Opus) 검색이 이메일 전용 + 검색 버튼 클릭 필요 + 탭과의 관계 불명 → Enter 검색, 검색 범위 표시
- **[MED]** (Opus) 활성/만료됨/폐기됨 탭에 건수 배지 + `role="tablist"`

### `/models`
- 최근 리디자인 완료 (확장 패널/3줄 식별자/$1M 단가)
- **[MED]** (Opus) `Opus 5.5 [1m]` vs `Opus 5.5`, `Opus 4.6` vs `Claude Code · Opus 4.6` 같은 중복처럼 보이는 엔트리 — display_name 누락분 채우기 또는 alias 중심 표시 + provider/family 그룹핑
- **[MED]** (Opus) 컬럼 헤더 "입력단가/출력단가"와 셀 "$4.00/M" 단위 불일치 → 헤더에 "(USD/1M)" 명시. 캐시 정밀도 통일 ($0.200 vs $4.00)
- **[MED]** ◆ 컨텍스트 `—`가 "미등록"인지 "해당없음"인지 불명 → 툴팁
- **[MED]** (Opus) 검색/필터 없음 (22+행)
- **[LOW]** (Opus) 매 행 빨간 "비활성화" 버튼 노이즈 → 오버플로 메뉴로. 비활성화 다이얼로그에 참조하는 앱/다운그레이드 체인 표시 제안
- **[LOW]** (Opus) "단가 동기화" 마지막 동기화 시각 표시

### `/monitoring`
- **[HIGH]** 런타임 에러 배지 확인 (TOP #7)
- **[MED]** (Opus) "활성 모델 22"가 카탈로그 수치 → "1시간 내 호출된 모델"로 또는 제거
- **[MED]** 요약 1h vs 로그 24h 창 불일치 → 선택 가능하게 또는 라벨
- **[MED]** ◆ 빈 상태 3연속 → 통합/컴팩트 + 다음 행동 링크
- **[LOW]** "0ms" 지연을 0 요청 시 `—`로 (빈 측정치가 실측처럼 보임)
- **[LOW]** 갱신 시각에 timezone 없음, 자동/수동 새로고침 컨트롤 없음
- **[LOW]** (Opus) 본문 로깅: 누가/언제 켰는지 + ON 상태 경고 강화

### `/analytics`
- **[HIGH]** 차트 한글 깨짐 (TOP #1)
- **[MED]** (Opus) "그룹화 기준"이 breakdown 차트에만 적용 — 추이 차트는 팀 고정 → 범위 라벨 또는 양쪽 적용
- **[MED]** (Opus) "기간 선택 ▾"와 "직접 입력" 이중 컨트롤 → 통합
- **[LOW]** "모델별 비용 상세"가 주 액션처럼 보임 → 링크/우측 배치
- **[LOW]**보내기 메뉴에 내용(포맷/적용 필터) 미표시

### `/analytics/models`
- **[HIGH]** 비용-토큰 검산 불가 + 라벨 중의성 (TOP #5)
- **[MED]** (Opus) 클라이언트 필터 부재 — 대시보드엔 있는데 드릴다운 페이지에 없음
- **[LOW]** "← Analytics" 영문 (페이지명은 "분석") — i18n
- **[LOW]** 지연 "7922ms" → "7.9s" + 평균/P50 명시

### `/chat`
- **[MED]** (Opus) "NDS-02 업데이트 스크립트" — 내부 용어 + 실행 방법 없음 → 실제 명령/문서 링크
- **[LOW]** 사이드바 미노출 — 의도면 OK, 아니면 배포 여부에 따라 nav 조건부 표시

### `/cli`
- **[HIGH]** env placeholder + 복사 버튼 (TOP #4)
- **[LOW]** Windows 스텝 env-var 누락, Cowork/Codex 미언급

### `/my`
- **[MED]** (Opus) ADMIN에게 "관리자에게 문의하세요"는 부조리 → ADMIN은 `/`로 리다이렉트 또는 "팀 리더·개발자 전용 페이지" 안내
- **[TODO]** TEAM_LEADER/DEVELOPER 역할 화면은 이번 캡처 범위 밖 — 별도 캡처 필요

### `/rate-limits`
- redirect('/users') — 소유권 일원화 OK. TOP #2의 deep link/토스트와 함께 개선

### `/403`
- **[MED]** (Opus) "홈으로 돌아가기"만 있고 역할별 대체 목적지 안내 없음

---

## 공통/디자인 시스템 이슈

| 항목 | 심각도 | 양쪽 합의 |
|------|--------|----------|
| 헤더 브랜드 중복 → 페이지 제목/브레드크럼으로 | MED | ◆ |
| 사이드바 `w-64` 고정, lg 미만 접기 없음 | MED | Devin |
| 모델 표시 규칙 불일치 (display_name/alias 페이지마다 다름) | HIGH | Opus |
| 팀 이름 표기 불일치 `NDS-Developers` vs `NDS_Developers` | MED | Opus [✓확인됨] |
| 기간 선택기가 페이지마다 다름 (공통 PeriodSelector로) | MED | Opus |
| 한영 혼용: "API Keys", "← Analytics", "Input 토큰", "VK", "CC" | MED | Opus |
| 빈 지표가 실측처럼 보임 (0ms, 0%) | MED | Opus |
| raw enum 배지 (BEDROCK_RUNTIME_OPENAI) | LOW | Opus |
| native select와 segmented control 스타일 불일치 | LOW | Opus |
| skip-to-content 없음, tablist role 누락 | LOW | ◆ |

---

## Opus 리포트와의 차이 정리

- **Opus의 "×1000 단가 오류" 주장은 검증 결과 기각** — `cost_per_1k_tokens`는 캐시 포함 실효 단가이고 `fmtPricePerM` 변환은 정확. 다만 라벨 모호성+캐시 컬럼 부재라는 실질 문제는 유효 → TOP #5에 반영.
- **Opus가 더 잘 잡은 것**: 페이지 간 수치 불일치(캐시 토큰 98% 미표시, 활성 모델 이중 의미, API Keys 0 vs 요청 14), `/budgets` $0.00 모호성, `/apps` 저장 방식 혼재+기본모델 검증 부재, 모델 표시 규칙 불일치, 팀명 표기 차이.
- **Devin이 더 잡은 것**: `/users` 초기 데드엔드(겹침), 사이드바 고정폭, `/keys` 빈 상태 링크 부재, skip-to-content.
