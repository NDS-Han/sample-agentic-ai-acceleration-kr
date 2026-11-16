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

## 머지 diff 심층 리뷰 (후속, `589dd6a2`)

**추출 충실성**: `provision_user` = 원 exchange의 1-7단계를 그대로 이동
(JWT 검증→클레임 추출→Cognito 이메일 enrich→그룹 게이트→팀 해석→역할 도출→
upsert→서비스계정 거부→transfer→활성 검사). exchange는 VK 발급·Redis 캐시
하이드레이트만 추가 수행 — `sso_session_expires_at`은 발급에서만 사용,
provision 미전달 정상. 에러 매핑(401/403/503)·레이트리밋
(`rl:auth_exchange:{ip}` 공유 10/min, bearer 추출 후 적용) 동일.

**`default=None` 크래시 실증**: `questionary.text(default=None)`이
prompt_toolkit `Document.__init__`의 `len(None)`에서 `TypeError` — `or None`
패턴은 `aaa6760e`(configure 도입)부터 존재해 미설정 선택 필드가 있는
yaml로 configure를 돌리면 해당 질문 렌더 시점에 마법사가 죽고 답변이
유실되던 결함. PR 수정 정당, 20개소 전부 `str(_g())` 패턴으로 확인.

**compose env 전파**: `${VAR:-}`는 `gen/<env>/.env`에서 interpolate.
`merge_env`의 `_MANAGED_PREFIX_ANCHORS`(OIDC_→OIDC_ISSUER_URL) 규칙상
운영자 수동 OIDC_REDIRECT_URI는 OIDC 켜진 동안 보존·끄면 수렴 — 올바른
계약. `SECURE_COOKIES`는 proto 자동감지(`secureCookieFlag`) 실패 시
강제 오버라이드 — 외부 HTTPS 프록시 용도와 일치.

**콜백**: provision 호출이 토큰 검증 후·쿠키 발급 전, `cookieToken`(검증된
세션 토큰) 사용, `AbortSignal.timeout(10s)`, 실패 시 403/503 fail-closed
(쿠키 미발급) — 신규 테스트 2건이 순서·미발급을 고정.

**후속 수정(이번 리뷰)**:
- `domain_name` 질문에 `validate` 추가 — route53-acm + 빈 도메인이 wizard
  끝 SchemaError(`schema.py:636`)로 답변 유실되던 경로 차단
- ad-sync.md 표에 "SAML 사용자 커버" 컬럼 복원 — 병합본이 산문으로만
  옮겼던 정보를 표로 복구

**잔여 관측(수용)**:
- capture가 `adminUi.env`의 `OIDC_REDIRECT_URI`/`SECURE_COOKIES`를 cfg로
  읽지 않음 — eks→compose 채택 시 `.env` 수동 캐리오버 필요(구조적 —
  cfg 필드 자체가 없음). feature_notes가 안내 문구 출력
- 콜백이 provision 응답 `{user_id, role}`을 쓰지 않고 `/admin/my/profile`
  재조회 — 역할 조회가 본래 목적이라 정상
- cold JWKS + Cognito AdminGetUser 합산이 10s 타임아웃을 넘는 극단 케이스는
  503 → 재로그인 안내로 수용
