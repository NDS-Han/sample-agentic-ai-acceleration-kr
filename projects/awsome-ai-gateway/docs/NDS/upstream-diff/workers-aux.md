# 워커·부속 서비스 — upstream 대비 변경 전체

## 1. notification-worker (+5,133)

| 변경 | 내용 |
|---|---|
| **SES 발송 채널** | worker 코드 + 로케일 템플릿, SES 발신 provider + IRSA·provider 전환 도구(update-scripts yq 재작성) |
| **notifications:system** | 무발행 채널 연결 — degradation/provider_error 알림 경로(R40) |
| **VK 수명주기 알림** | 키 만료·폐기 알림, 만료 키 purge(기본 off) |
| **예산 알림** | `budget_alert_thresholds`(0041) 소비 — 임계값 도달 시 발송 |
| dedup | `notification_logs_dedup`(0040) — 중복 발송 방지 |
| 로그 위생 | 예외 메시지의 이메일 마스킹(`mask_emails_in_text`) |

## 2. cost-recorder-worker (+4,057)

- 예산 정산 정합 — 장문 컨텍스트 티어(0042/0047) 요금 계측,
  Haiku 5.5 비용 정합
- LiteLLM 단가 동기화 소스 소비, `LITELLM_COST_MAP_URL` 폴백
- daily_aggregates bigint(0044), key_prefix(0051)·session_id(0054)
  사용 로그 컬럼 반영

## 3. litellm-pricing-lambda/ — 신규

- LiteLLM 단가 조회를 **Lambda 프록시로 격리** — 직접 호출 제거,
  미배포 시 admin-ui 비활성화
- `LITELLM_COST_MAP_URL` cost-map 폴백 모드
- deploy 프로비저너(`pricinglambda.py`)가 배포·동기화 — NDS-01

## 4. admin-chat-agent (+1,758)

- **NDS 자동 프로비저닝 승격** — `chatagent.py`가 AgentCore Runtime
  생성·등록, `chat_reader` DB 비밀번호 자동 동기화
- 메시지 리포트(migration 0057) + admin-ui `/agents` 페이지
- Runtime teardown을 NDS 수명주기에 편입(수동 단계 제거)

## 5. mock/ + loadtest/ — 신규

- **`mock/`** — mock-bedrock 서비스: 부하 테스트에서 Bedrock 비용 $0
- **`loadtest/`** — k6 시나리오·k8s 잡·SSE-hold 로드젠
  (`sse_loadgen.py`) — 10k boot-storm·executor 포화 등 실측 부하

## 6. alerting·관측 인프라 (terraform 모듈)

- `alerting` 모듈 — SNS + CloudWatch alarms + Fargate log shipping
- `bedrock-invocation-logging`·`body-logging` 모듈 — 로그 수집 계층
- ALB 알람 자동 발견 — apply가 ingress→elbv2 dimension 주입
- 비용·예산 알림은 notification-worker가 소비

## 7. BI Insight (ecs 타깃)

- `bi_insight` 기능 — AgentCore Runtime 프로비저닝 + DB 비밀번호
  동기화, 미배포 시 admin-ui 숨김
