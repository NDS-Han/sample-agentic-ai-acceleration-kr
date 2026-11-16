# PR #4 리뷰 — `t0 배포 후 수정 사항` (siwoolyyy/nds/dev → nds/dev)

외부 PR의 코드 리뷰·보안 판정·충돌 해소 기록. 머지 커밋 `1ab19b29`.

## 변경 개요

| 커밋 | 내용 | 채택 |
|---|---|---|
| `9f50739b` | 마법사 questionary 빈 입력 기본값(`or None` → `str()`) | ✅ |
| `bdeee5cf` | Compose admin-ui OIDC env 전달 | ✅ |
| `63dc81ec` | `provision_user` 추출(exchange 공유) | ✅ |
| `7f7bdf0e` | `/v1/auth/provision` 엔드포인트 + admin-ui 콜백 호출 | ✅ |
| `c8bae5c6` | ad-sync.md provision 문서 | ✅(우리 sync 내용과 병기) |

## 기능 판정 — provision 도입 타당성

해결하는 문제: IdP(풀 그룹·SAML `custom:groups` 무관)에는 있는데
`auth.users`에 없는 사용자가 admin-ui 첫 로그인 시 `user_not_provisioned`(403).

기존 `sync-cognito`는 **수동·관리자 실행**(스케줄러 미등록)이라 "콘솔 추가 →
자동 등록"은 폴링·Cognito 트리거 Lambda·EventBridge 중 하나를 신설해야 가능 —
provision은 로그인 시도 자체가 트리거라 해당 인프라 비용이 0.

| 경로 | 역할 |
|---|---|
| `sync-cognito` | 배치/정합성 — 전체 열거·`custom:groups` 병합·stale 정리 |
| `/v1/auth/provision` | JIT — 첫 로그인 자가 등록(VK 미발급) |

## 보안 판정 — 신규 취약점 없음

`/v1/auth/provision`은 **exchange의 엄격한 부분집합**:

- 토큰 검증(JWKS 서명·iss·aud)·그룹 게이팅·비활성 거부를 `provision_user`로
  공유 — 요청 바디에 주체 정보 없음(임의 사용자 주입 불가)
- 유효 토큰 소지자가 새로 얻는 능력 없음 — 원래 `/exchange`로 행 생성 +
  VK까지 얻을 수 있었고 provision은 자격 미발급
- 레이트리밋 `rl:auth_exchange:{ip}` 공유(10/min) — 공격면 확대 없음
- admin-ui 콜백은 provision 실패 시 세션 쿠키 미발급 — fail-closed

## DB 커넥션풀 판정 — 외부 I/O가 커넥션을 잡지 않음

`provision_user` 실행 순서: JWKS 검증 → Cognito `AdminGetUser` → **첫 DB**
(팀 인덱스 조회) → upsert. `AsyncSession`은 lazy라 외부 네트워크 대기 구간에
풀 커넥션 미점유. upsert 경로는 이미 10k boot-storm 부하테스트로 `list_all_teams`
→ 단건 인덱스 SELECT + savepoint 레이스 처리로 다듬어진 코드 — exchange보다
엄격히 저렴(VK INSERT·예산 조회·Redis 하이드레이트 없음).

## 충돌 해소 기록

| 파일 | 해소 |
|---|---|
| `cli.py` | 우리 재작성(검증·도메인 보존 로직) 유지 + PR의 `or None` 제거 20개소만 적용 |
| `compose.py` | admin-ui env 5종 병합 + 우리 `NEXTAUTH_URL` 도메인 게이트·dev_login 경고 유지. feature_notes는 PR 문구(OIDC_REDIRECT_URI/SECURE_COOKIES 안내) 채택 |
| `ad-sync.md` | sync 전체열거·attr_groups 설명 + provision 표·403 복구 안내 병기 |

추가 수정: provision `request_id`에 `x-request-id` 헤더 폴백 추가(exchange와 동일).

## 지적 사항(수용 or 잔여)

- `ADMIN_EMAILS`가 admin-ui env에도 주입 — admin-ui는 미독독이나 무해, PR 충실성 위해 유지
- provision 429 시 콜백이 403 매핑 — fail-closed라 수용
- NAT 뒤 다수 사용자의 레이트리밋 공유 — 운영 인지 사항

## 검증

- deploy 스위트 478 tests ✅
- admin-api OIDC/provision 관련 64 tests(`test_web_oidc_provision` 신규 포함) ✅
- admin-ui `authLoginFlow.test.ts` 67 tests ✅
