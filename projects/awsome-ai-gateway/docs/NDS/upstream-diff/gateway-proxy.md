# gateway-proxy — upstream 대비 변경 전체

가장 큰 코드 델타(+48,837 라인). VK/JWT 인가·레이트리밋·예산·회계의
기본 골격 위에 **web search 루프·body logging·Claude Code 모드·CAP
강제·계정 라우팅·장문 티어**를 추가했다.

## 1. Web Search 루프 (`web_search`)

| 변경 | 내용 |
|---|---|
| 네이티브 블록 | 검색 흔적을 Anthropic 네이티브 `tool_use`/`tool_result` 블록으로 기록 (1.0.70-native) |
| 되돌아온 결과 | 검색 결과 발췌를 본문 600자→트림 전체(1500자)로 재작성, "전문은 그 턴에 봤다" 메모 (1.0.72/74/75) |
| 균형 조정 ①~⑤ | 품질 우선 균형 (1.0.67-balance) |
| 클라이언트 도구 공존 | web search + 클라이언트 자체 도구 혼합 턴 실행·soft final 턴 (1.0.78-clienttools) |
| tool_search | `ENABLE_TOOL_SEARCH` 기본 활성 + `--tool-search` 플래그 |
| 비용 상한 | EC2 values에 web search 비용 상한 4개 + 정산·상태전파·억제 정합 |
| 프로비저닝 | AgentCore Gateway URL 자동 프로비저닝·values 반영 |
| TTFT | 클라이언트 체감 TTFT 기록(web search 경로) |

## 2. Body Logging

- 요청/응답 본문 로깅 — **관리자 런타임 토글**, 위험 방향 가드
- OpenAI 방언 + web-search 경로 포함
- **싱크: Firehose → S3**, 모든 계층에서 기본 off (body-logging 모듈)

## 3. Claude Code 모드·정책 조각

- **Auto mode 서버 분류기** — beta 2개 + safeguards 전달, 웹 검색
  응답에 판정 유지
- `GET /v1/policy` — policy-helper가 VK로 받는 정책 조각, 거버넌스
  허브 조각을 모델 조각 아래 병합, **허브 불통 → 503**, 허브 조각 키는
  허용 목록만 통과
- 조각의 `null`이 기본값 키를 지우도록 merge 확장
- inline-tools·thinking-display-updates 전달, advisor를 가리키는
  tool_addition 제거
- safeguards passthrough

## 4. CAP 약정 강제 (gateway enforcement)

`cap_u = A_u × D` 산식, `limit=0` 차단, 계층 조합
(D-3,8,10,12,14,20) — 팀 기본 유저 cap(migration 0039)과 연동.

## 5. 모델·계정 라우팅

- **모델 레벨 AWS 계정 라우팅** — 전 디스패치 경로에서 모델별
  x-account ARN 선택(migration 0049), 클라이언트 라우팅 프로필
  편집 + DR runbook
- 허용 목록: user > team > org 계층 `allowed_clients` 해석(0038 소비자)

## 6. 장문·요금 티어

- **272K long-context tier** — 명시적 요금 + per-request tier audit,
  Haiku 5.5 장문 요금 + cost-worker 정합
- 단가 NUMERIC(12,8) 정밀도, 100K 초과 구간 단가

## 7. 운영성

- **bedrock executor inflight → custom-metric HPA 신호** — 포화 계측
  + 스케일링 문서
- 예산 복구·circuit breaker·자격증명·바디로그 보조 서비스군 정합
- `Authorization: Bearer `(후속 공백) IndexError → 401 방어
- session_id(0054)·key_prefix(0051) 사용 로그 컬럼
- 요청별 tier audit·web search·비용 필드 회계 정합
