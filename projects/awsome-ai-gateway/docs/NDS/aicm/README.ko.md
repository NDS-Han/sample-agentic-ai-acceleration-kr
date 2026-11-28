# AICM 통합 가이드

**한국어** · [English](README.md) · [NDS 문서](../README.md)

[AICM OpenAPI 계약](aicm-v1-openapi.json)과 [클라이언트 계약](aicm-client-contract.json)이 실제 요청 및 응답 스키마의 기준입니다. API는 `AICM_API_ENABLED=true`인 환경에 마운트됩니다. 내부 prod에서 통합을 검증하며, 공개 dev는 기능이 꺼진 상태를 확인하는 환경입니다.

## 연결

- 기본 주소는 `https://<gateway-host>/aicm/v1`입니다. 계약에 포함된 기존 `/admin/...` 경로를 그 아래에서 사용합니다.
- 사람이 로그인한 관리자가 `POST /admin/service-tokens`에서 AICM scope를 지정해 통합 토큰을 발급합니다. 호출 시 `Authorization: Bearer svc-...` 헤더를 사용합니다.
- 지원 scope는 `users:read`, `users:write`, `users:sync`, `budgets:read`, `budgets:write`, `models:read`, `tokens:read`, `tokens:rotate`입니다. 발급할 때 CIDR 및 요청 한도를 지정할 수 있습니다.
- AICM 토큰은 계약의 미러 경로에서 사용합니다. 일반 관리자 JWT와 범용 서비스 토큰은 각 인증 흐름에 맞는 별도의 주소를 사용합니다.

## 정책과 예산

- 앱·모델 허용 목록의 빈 배열은 전체 허용입니다. 금액은 소수점 네 자리까지 지원합니다.
- 팀 예산을 `unlimited`로 선언하면 계약에 정의된 최대 금액을 저장합니다. 일반 한도는 팀의 실제 예산값을 지정합니다.
- `PUT /admin/budgets/spent`는 누적 사용액을 절대값으로 설정합니다. App-group은 `claude-code`와 `cowork`에 1:1로 적용되며 앱별 한도와 그룹 한도 중 작은 값이 유효합니다. 0은 해당 앱 차단이며, 삭제하면 독립적인 앱별 한도가 다시 적용됩니다.
- 관리자·서비스 계정·시스템 사용자는 통합 주체의 변경으로부터 보호됩니다. 보호 대상이 포함된 사용자 예산 배치는 전체가 거부되며 감사 기록에 남습니다.

## 오류, 재시도와 운영

- `401`은 자격 증명 상태를 점검한 뒤 한 번 재시도합니다.
- `408`, `429`, `5xx`는 지수 백오프를 사용해 최대 세 번 시도합니다. `429`에서는 `Retry-After`도 확인합니다.
- `POST /admin/service-tokens/rotate`는 자동 재시도를 하지 않습니다. 발급·회전 후에는 토큰 원문을 승인된 비밀 저장소에서 관리합니다.
- 요청에 `x-request-id`를 전달합니다. 전체 Cognito 동기화는 오래 걸릴 수 있으며 백엔드에서 동시 실행을 제한합니다. 현재 오류 형식과 사례는 [영문 계약 안내](README.md)를 참고합니다.
