# R41 — 배포/운영 도구 + 문서-코드 일치 2차

날짜: 2026-10 · 대상: docs/NDS/deploy/*(cli·schema·tiers·render 3종·apply 2종·bootstrap·
doctor·capture·websearch·pricinglambda), docs/NDS/*.md 전체.
베이스라인: deploy tests 213/213.

## 수정

### 1. `deploy configure` 가 묻지 않는 필드를 조용히 삭제 (cli.py)

`_collect_doc` 의 features/notifications 재구성이 질문한 키만 써서, 수동 선언된
값이 configure 한 번에 유실됐다 — oidc 필드 보존(기존 코드, line 215 주석)과
정확히 같은 종류의 결함.

유실되던 값:
- `features.dev_login` — 체크박스 선택지에 없어 항상 false 로 리셋. 켜둔 배포에서
  configure → apply 하면 dev 로그인이 조용히 꺼진다(운영 의도와 반대 방향은 아니지만
  선언-실행 불일치).
- `features.notifications.smtp_port` — 질문 없음 → 587 로 리셋. 비표준 포트 SMTP
  환경에서 알림이 조용히 깨진다.
- `features.notifications.sender_name` — 질문 없음 → 삭제.
- 향후 스키마에 추가될 feature 플래그도 같은 경로로 유실될 구조.

수정: 두 곳 모두 "묻지 않은 키는 기존 dict 에서 보존" 규칙으로 통일 —
`prev_notif` 전체 보존 루프 + features 의 미질문 플래그 보존 스프레드.
회귀 테스트(`test_configure_preserves_unprompted_fields`)에 3개 단언 추가.

### 2. eks `apply` 만 post-apply 안내 누락 (cli.py)

`_post_apply_notes`(스모크 테스트 안내 + 시드 라우팅 프로필의 샘플 크로스계정 ARN
경고)이 compose·ecs·bootstrap-eks 에는 있는데 `cmd_apply` 의 eks 분기에만 없었다.
bootstrap 이 아닌 apply 로 eks 를 처음 올리는 경로(캡처 채택 후 첫 apply 등)에서
"요청 전부 AssumeRole 실패" 함정 안내가 안 나온다 — 동일하게 연결.

### 3. compose teardown 패널에 pricing Lambda 잔여 미안내 (cli.py)

ecs/eks 는 apply 가 boto3 로 만든 Lambda 를 teardown 이 정리하지만, compose 는
프로비저닝하지 않으므로(ops/NDS-01 수동 생성) teardown 도 건드리지 않는다 —
소유 경계는 맞으나 패널의 "남는 것" 목록에 없어 수동 생성분이 잊힐 수 있었다.
`features.pricing_lambda` 시 패널에 정리 방법을 안내하도록 추가.

### 4. 문서-코드 불일치 정정

| 파일 | 정정 |
|---|---|
| `update.md` | "모든 backend 에서 pricing_lambda 도 함께 삭제" → ecs/eks 만 정리, compose 는 apply 가 만들지 않아 teardown 도 미개입(수동 `aws lambda delete-function` 안내) |
| `gateway-yaml.md` | `deploy.release`/`deploy.namespace`/`deploy.tf_env_dir`(eks 전용 키) 미문서화 → 예시 블록에 추가. `network.mode=private` 이 eks 외에 ecs(ALB internal)에도 적용됨을 명시 |
| `install-compose.md` | `features.observability` 시 관측 스택이 루프백 바인드임과 Grafana SSH 터널 접근법(`GF_SECURITY_ADMIN_PASSWORD` 위치 포함) 안내 추가 — R35 변경의 운영 반영 |

## 검증된 정상 (변경 없음)

- **커맨드 표면**: init/configure/validate/render/bootstrap/apply/doctor/
  client-values/teardown 전부 add_parser 와 README/gateway-yaml.md 일치.
  `./deploy` → `docs/NDS/deploy.sh` → `python -m deploy` 체인 정상.
- **doctor**: compose(산출물·필수 시크릿·서비스 상태·alembic head·env drift·
  Caddyfile validate), ecs(terraform plan -detailed-exitcode 로 선언+라이브
  통합 drift, service running/desired, migration task exitCode, /health
  엔드포인트), eks(helm status, deployment ready+이미지 태그 핀 drift, migration
  hook Job→실패 시 helm history 폴백, inbound-cidrs 미설정=침묵 개방을 drift 로
  검출, 클러스터 버전 skew, tf plan) — 세 경로 모두 위험 신호를 HIGH 로 표면화.
- **teardown**: 환경 이름 타이핑 확인(y/n 아님), ecs db_safeguards 해제 절차 +
  스냅샷 경고, eks `--infra` 의 helm→인프라 순서와 helm status 실패 유형별
  분기(release 부재 vs 접속 실패 구분 — 후자에서 인프라 삭제 중단),
  websearch/pricinglambda 의 terraform-밖 리소스 별도 정리.
- **apply**: ecs 의 extra-vars.json(발견값이 gateway.yaml 을 오염시키지 않음)과
  2-phase NEXTAUTH_URL, eks 의 dynamic --set(terraform output)·module-scoped
  targeted infra apply·2-phase ALB nextauthUrl·구 ES 정리.
- **bootstrap**: 시크릿 "없을 때만 생성"(`/app` 랜덤 4키, `/redis` auth_token
  복사), `/db` 는 검증만(수동 create 명시적 거부 — master_password 유실 방지).
- **capture**: 못 읽는 값은 빈칸+notes 의 정직 계약 — compose/ecs/eks 전 경로.
- **schema**: unknown 키 거부, 티어↔타겟 조합 강제(compose=t0, ecs=t1/t2,
  eks=t2/t3), cloudfront-temp compose 거부·ecs 는 none 수렴+feature note,
  allowed_cidrs 빈/0.0.0.0 경고, ecs DB 커넥션 예산(worst>천장=에러,
  steady>바닥=경고).
- **문서 정합**: operations.md §3 라우팅 프로필(SQL 전용 수정 — 실제 라우팅 API가
  조회+web-search 토글뿐인 것과 일치, 시드의 claude-code/cowork 크로스계정 ARN·
  codex NULL 도 확인), domain.md 의 Caddy 호스트 규칙=render_caddyfile,
  install-ecs/eks 의 bootstrap 단계=bootstrap.py 실제 순서, install-compose SG
  표(R35 루프백 바인드와 정합 — 관측 포트 미기재가 정답), requirements.txt
  (yaml/rich/questionary/boto3).
- **tiers.py**: ecs_service_sizing 의 스트림 consumer desired_count=1 핀·
  max_count 재계산, AUTOSCALE_MAX_FACTOR 가 트래픽 서비스만.

## 판정 보류

- `bi_insight`(AgentCore Runtime)은 세 렌더러 모두 "미구현/비대상"을 정직하게
  노트로 보고 — 기능 미완이지 거짓 선언은 아님. R42 채팅 에이전트 검토 시 재평가.
- ecs `cloudfront-temp` 는 none 수렴+노트 — CloudFront 모듈 부재는 인지된 갭.
- compose `network.mode=private` 미지원은 validate 경고로 표면화됨 — 호스트
  방화벽은 문서(operations.md §2) 안내 범위.
- observability 의 ecs/eks 미지원은 validate 경고로 표면화됨 — 의도된 compose 전용.
