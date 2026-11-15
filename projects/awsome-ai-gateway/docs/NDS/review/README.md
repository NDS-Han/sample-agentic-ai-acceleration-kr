# NDS 배포 도구 — 기능별 심층 리뷰 레포트

`docs/NDS/deploy/` (NDS deploy 도구, ~11k LOC)에 대해 진행한 20라운드의
독립 적대 리뷰 결과를 기능 영역별로 정리한 레포트 모음이다.

## 리뷰 방법론

- 각 라운드는 **독립 리뷰어(서브에이전트)가 코드를 직접 추적**해 발견 →
  주 리뷰어가 각 발견을 재검증 후 수정 → 회귀 테스트 추가의 순환.
- 초점: **파괴적 동작**(리소스 삭제/공개 전환/이미지 롤백/잠금),
  **오타겟**(다른 env·release·state에 쓰기), **시크릿 누출**,
  **신규 설치↔업데이트 경로 불일치**, **캡처/채택 정확성**.
- 매 라운드 수정은 `deploy/tests/` 회귀 테스트로 고정 —
  최종 **454 tests 통과**.

## 라운드 이력 (git log --oneline)

| 라운드 | 커밋 | 주요 대상 |
|---|---|---|
| 13차 | e79ee3a2 | ingress 라벨 fall-through 회귀, pricing fn_name, teardown Runtime 누수, SecretStore kind, sanitize 강화 |
| 14차 | 95530406 | namespaced SecretStore SA 결함, _live_deploy_env 라벨 매칭, human_owned 전 채널, sanitize oidc |
| 15차 | d6188ceb | doctor 폴백 오흡수, human_owned 인용/JSON, env:null 가드, sanitize CRLF/flow/블록스칼라, ECR 태그 fail-fast, dev_login+oidc 수렴 |
| 16차 | c559d592 | admin_internal 드롭, selfsigned 데드락, nextauthUrl 게이트, cognito_groups 병합, 차트 기본 시크릿, cert 우선순위, stale R53 정리 |
| 17차 | 4e2028b4 | 프로비저너 env 스크럽, 죽은 리소스 재사용, trust 정책 수렴, status_url READY 요구, teardown 격리 |
| 18차 | 0df26dde | capture 인스턴스 필터, 사이드카 오염, ECS ALB scheme, unknown-vs-empty, 스키마 강화, migration DB 예산 |
| 19차 | e3c1a209 | configure 필드 소실(allowed_roles/zone_id/capacity_tier), 캡처본 소스, compose dev_login fail-closed, terraform init -reconfigure, teardown env_dir 가드 |
| 20차 | 40782825 | gen overlay 경로(alert_emails/infra 유실), bootstrap stale cfg, plan/apply/doctor 항상 reconfigure, ECS backend 불일치 경고 |
| 21차 | 7e6d5a54 | 기능별 독립 검증 3인조 — teardown 완전성(OOB 잔여·인라인 정책·env_dir 전 채널 가드·apply 가드·빈 outputs 차단), capture 채택 안전(compose unknown·internal_api·ALB 폴백·도메인 게이트·env_dir 무효화), bootstrap/apply 수렴(persist split-brain·migration 폴백 ECR·null 섹션·ecs lock table·oidc 잠금 경고) |
| 22차 | d2d03565+후속 | 3인 리뷰 재투입 — cli 커맨드 표면(wizard 스칼라-리스트 소실·비질문키 보존·원자적 쓰기·absorb 백업), ecs 경로 수렴(immutable push·force_delete·stale URL·pricing 게이트·init 순서·wait 판정·빈 state 중단·삭제유예 가드·cidrs fail-closed 정정·extra-vars preview), doctor(init rc·readiness 우선·미검증 WARN·FAILED 라벨·digest 가드), eks(--context↔backend 불일치 차단·declared last-wins·teardown outputs 검증·namespaced SecretStore 소유 경계·capture provider unknown·tfstate retry persist) — **440 tests** |
| 23차 | 0a12da88 + 511e22ac + 71ffadb9 + f729d151 | 3인 리뷰 재투입 — ①22차 diff 회귀(교차타깃 wizard 저장불가·bootstrap alias 선행 hijack·teardown helm 선행·비인용 environment) ②잔여영역(.env declared-off 수렴→OIDC 잠금/websearch 유출·eks allowedIamRoles 미방출→STS 403·도메인 mode별 scheme 오안내·tfvars/backend.hcl 주입·공유버킷 lifecycle 덮어쓰기·JSON tfvars 채널 누락·questionary default 크래시·plan 의 pre-consent alias·비-ECR 레지스트리·selfsigned className) ③신규 diff 회귀검증 — **454 tests** |
| 24-25차 | e7f5db28 + (이번) | 3인 리뷰 재투입 — ①websearch/chatagent 프로비저너(dead/deleting 상태·find_runtime 레이스·env/image drift·ambient IMAGE_TAG 스크럽) ②전 라이프사이클(merge_env prefix 와일드카드가 SMTP_USERNAME/OIDC_EMAIL_CLAIM 운영자키 삭제·tfvars_clean `<<-` 들여쓰기 종결자·`.match`+`$` 후행개행 우회→fullmatch·compose/Caddyfile 검증-전-쓰기·teardown ctx 미해석 fail-open·eks feature-off 증거게이트·release/namespace 변경 구release 고아화·upsert_env_file 원자화·chat_name 프리패치 게이트·인용 heredoc 구분자·azs 상한/무효CIDR) ③신규 diff 회귀검증+프로비저너 재검증(schema `str(None)`→`"None"` 오염→_opt_str·sizing 키 sorted TypeError·Route53 외부 ALB 레코드 삭제→소유검증·UPSERT 전 기존레코드 경고·moved/import 플랜 스코프우회·helm --cleanup-on-fail 버전게이트·wait_services PRIMARY만·extra-vars 원자화·pending-deletion project 파생·fullnameOverride·persist 0644 창→0600·ECR immutable skip/레이스·safeguards 스크립트 https 강제·비JSON 200·CRLF SSE) — **464 tests** |

## 기능별 레포트

| 레포트 | 범위 | 판정 |
|---|---|---|
| [gateway-yaml-schema.md](gateway-yaml-schema.md) | 스키마·검증·init/configure wizard·필드 보존 계약 | 수렴 |
| [bootstrap.md](bootstrap.md) | 최초 설치 — scaffold·tfstate·env_apply·시드·이미지·온보딩 | 수렴 |
| [eks-apply.md](eks-apply.md) | helm 적용·managed overlay 화이트리스트·프로비저너 배선·ECR | 수렴 |
| [domain.md](domain.md) | 도메인 부착 — Route53/ACM/selfsigned-temp/admin_internal | 수렴 |
| [oidc-authn.md](oidc-authn.md) | Cognito/OIDC/AD 연동·dev_login 계약·service keys | 수렴 |
| [capture-adopt.md](capture-adopt.md) | 기존 배포 캡처→검토→채택 | 수렴* |
| [doctor.md](doctor.md) | doctor 점검 — 준비성·드리프트·위험 감지 | 수렴 |
| [teardown.md](teardown.md) | 삭제 경로 — helm/provisioner/terraform | 수렴 |
| [features.md](features.md) | 기능 플래그 + boto3 프로비저너(web_search/bi_insight/pricing_lambda) | 수렴 |
| [sizing-capacity.md](sizing-capacity.md) | size_tier/capacity_tier/sizing·DB/Redis 예산 | 수렴 |
| [ecs-compose.md](ecs-compose.md) | ECS(배포 보류)·compose 로컬 경로 | 조건부 수렴 |

\* 캡처는 라이브 AWS/K8s 에서만 최종 검증 가능 — 코드 경로는 수렴.

## 공통 잔여 리스크

- **라이브 검증 미수행**: 모든 수정은 단위 테스트 + 코드 추적으로 검증됐다.
  실제 EKS 클러스터/AWS 계정에서의 end-to-end(bootstrap → apply →
  capture → teardown) 실행 검증은 별도 필요.
- **AWS API 페이지네이션**: 조회성 호출(`describe-*`)은 페이지네이션이
  없다 — 리소스가 수백 단위로 많은 계정에서 누락 가능(LOW).
- **희귀 YAML 형태**: sanitize/parser 가 커버하지 않는 극단적 YAML
  변형이 남아있을 수 있으나, 실패 방향은 전부 fail-closed.
