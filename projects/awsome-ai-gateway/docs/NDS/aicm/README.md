# AICM `/aicm/v1` 통합 가이드 (phase 0a)

이 디렉터리의 `aicm-v1-openapi.json`이 계약서다. phase 0a 범위는 다음 다섯 경로뿐이다.

- `POST /aicm/v1/service-tokens` — 사람 admin JWT로 통합 토큰 발급(평문 1회만 반환)
- `GET /aicm/v1/service-tokens` — 호출 토큰과 같은 rotate 계보 목록
- `POST /aicm/v1/service-tokens/rotate` — 토큰 자기 교체(구 토큰 24시간 grace)
- `POST /aicm/v1/users/provision` — Cognito 생성/ADFS 연결 + 게이트웨이 사용자 동기화
- `POST /aicm/v1/users/{user_id}/deactivate` — Cognito disable/sign-out + 사용자 비활성화 + VK 폐기

## 인증과 권한

`POST /service-tokens`을 제외한 모든 호출은 `Authorization: Bearer itg-...` 통합 토큰을 사용한다.
기존 `svc-` 토큰과 사람 JWT는 통하지 않는다. 토큰은 발급 시 부여한 scope만 가진다.

phase 0a scope:

- `tokens:read`
- `tokens:rotate`
- `users:provision`
- `users:lifecycle`

발급 시 `allowed_cidrs`를 지정하면 그 원천 대역만 허용된다. 토큰 계보(lineage)별로 read/write/lifecycle
고정창 레이트리밋이 적용된다.

## 멱등성

`rotate`, `provision`, `deactivate`는 `Idempotency-Key` 헤더가 필수다(1–128자 printable ASCII).
같은 키와 같은 요청을 다시 보내면 저장된 응답이 `Idempotent-Replayed: true`와 함께 반환된다. 같은 키의
다른 요청은 `422 idempotency_key_reused`, 진행 중이면 `409 idempotency_in_progress`다. 실패한 요청은
저장하지 않으므로 같은 키로 재시도할 수 있다. rotate 응답의 새 토큰 원문은 저장하지 않으므로 재전송은
`409 idempotency_secret_not_replayable`를 반환한다.

## provision 의미

`groups`에는 팀으로 해석 가능한 게이트웨이 OIDC 그룹이 하나 이상 있어야 하며 admin 그룹은 거부된다.
고객 Cognito 풀에 SAML IdP가 설정돼 있다면 `link`를 보내야 한다. 이미 ADFS로 로그인해 만들어진
프로필(`UserStatus=EXTERNAL_PROVIDER`)은 삭제하거나 연결하지 않고 `state=already_federated`로 보고한다.
Cognito 작업이 부분 성공한 뒤 실패하면 응답 `details.partial_state`를 보고 같은 요청을 재시도한다.

## 운영 설정

기능은 `AICM_API_ENABLED=false`가 기본값이며 false일 때 라우터가 마운트되지 않는다. 필요한 환경 변수와
IAM 권한은 wave의 `HANDOFF.md`를 참조한다.
