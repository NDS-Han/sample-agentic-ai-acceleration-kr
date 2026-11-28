# NDS 전환 변경사항 종합 — upstream 대비 기능 차이 목록

이 디렉토리는 upstream(`gonsoomoon-ml/sample-agentic-ai-acceleration-kr`)과
NDS 브랜치 사이의 **기능 단위 차이 전체**를 정리한다. 기준 커밋:

- **merge-base**: `e3a39757` (upstream PR #17 머지 — 2026년 초)
- **비교 시점**: 2026년 11월 16일 문서 작성 당시의 `nds/dev` (`5963902b`의 변경 기록)
- **규모**: `projects/awsome-ai-gateway` 기준 1560 파일, +269,782/−14,988 라인,
  커밋 ~1,384개

upstream은 merge-base 이후 거의 정지해 있어, 사실상 **모든 차이가 NDS 쪽
추가·변경**이다. 삭제된 top-level 디렉토리는 없다.

현재 절차는 [NDS 문서 목차](../README.md), 이후 upstream 반영 절차는 [upstream 동기화](../upstream-sync.md)를 참고합니다.

## 문서 구성

| 문서 | 범위 |
|---|---|
| [deployment.md](deployment.md) | NDS 배포 모델 — `gateway.yaml` 선언적 배포 도구, terraform 모듈, helm 차트, capture/doctor/teardown, 도메인 자동화, 용량 티어 |
| [identity.md](identity.md) | 인증·인가 — OIDC/Cognito/SAML-AD 연동, `/v1/auth/exchange`·`provision`, dev_login, 서비스 계정, 그룹→역할/팀 매핑 |
| [admin-api.md](admin-api.md) | 관리 API — 신규 라우터 5종, 감사·설정·바디로그·서비스 계정, DB 마이그레이션 0024–0057 |
| [admin-ui.md](admin-ui.md) | 관리자 콘솔 — 신규 페이지 5종, 전면 i18n, 정책 편집·예산·모니터링 UX |
| [gateway-proxy.md](gateway-proxy.md) | 게이트웨이 프록시 — web search 루프, body logging, Claude Code auto/safeguards, CAP 강제, 계정 라우팅, 장문 티어 |
| [clients.md](clients.md) | 클라이언트 — 직원용 인스톨러(Win/Linux), cowork-installer, gateway-cli v1/v2, policy-helper, OTEL |
| [workers-aux.md](workers-aux.md) | 워커·부속 — notification(SES)·cost-recorder·pricing-lambda·admin-chat-agent·mock-bedrock·loadtest |

## upstream에 없는 신규 top-level 구성요소

| 경로 | 내용 |
|---|---|
| `deploy` | `./deploy` 진입점 — `docs/NDS/deploy.sh` 위임 래퍼 |
| `docs/NDS/` | NDS 통합 문서 + **배포 도구 전체**(deploy/*.py, render/, tests/) |
| `deployment/` | terraform 모듈 12종·environments 3종, helm 차트, render 산출물, TUI |
| `installer/` | 직원용 CLI 인스톨러 — Windows(Inno Setup)·Linux(deb), packaging, site-config |
| `cowork-installer/` | Cowork용 별도 인스톨러 트리 |
| `policy-helper/` | Claude Code 정책 조각 도구 — baseline 병합, macOS 설치 |
| `litellm-pricing-lambda/` | LiteLLM 단가 조회 Lambda 프록시(직접 호출 격리) |
| `mock/` | mock-bedrock — Bedrock 비용 $0 부하 인프라 |
| `loadtest/` | k6·k8s 부하 시나리오 + SSE hold 로드젠 |
| `tasks/` | 작업 추적 문서 |

## 변경의 축 — 한눈에

1. **배포 방식 교체**: 사이트 고정 스크립트(`docs/us-llm-gateway/`, update-scripts,
   install-eks.sh 등) → **`gateway.yaml` 단일 선언 + `./deploy` 통합 도구**.
   compose(t0)·ecs(t1/t2)·eks(t2/t3) 3대상, bootstrap/apply/doctor/teardown
   수명주기, `doctor --capture` 역생성.
2. **멀티앱·거버넌스**: 앱 단위 모델 허용·예산·라우팅(allowed_clients,
   user>team>org 계층), CAP 약정 강제(cap_u=A_u×D), 모델·계정 레벨 라우팅.
3. **인증 확장**: Cognito OIDC + SAML/AD FS + dev_login 공존 — JIT provision,
   서비스 계정 장기키, 그룹→역할/팀 매핑.
4. **관측·비용**: web search 정산, body logging(Firehose→S3), 장문 컨텍스트
   티어 요금, LiteLLM 단가 Lambda, 예산 알림 임계값, notification SES.
5. **운영 도구**: 인스톨러·정책 헬퍼·chat-agent·mock/loadtest·doctor·
   캡처·마이그레이션 34개·리뷰 레포트(`docs/NDS/review/`).

각 문서는 "기능 → 무엇이 달라졌는가 → 왜(NDS 요구)" 순으로 정리한다.
