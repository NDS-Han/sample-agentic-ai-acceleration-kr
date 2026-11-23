# NDS 배포 도구 + 애플리케이션 — 기능별 심층 리뷰 레포트

`docs/NDS/deploy/` (NDS deploy 도구, ~11k LOC)에 대해 진행한 29라운드와
애플리케이션 컴포넌트(admin-api/gateway-proxy/워커/클라이언트) 최종 리뷰의
독립 적대 리뷰 결과를 기능 영역별로 정리한 레포트 모음이다.

## 리뷰 방법론

- 각 라운드는 **독립 리뷰어(서브에이전트)가 코드를 직접 추적**해 발견 →
  주 리뷰어가 각 발견을 재검증 후 수정 → 회귀 테스트 추가의 순환.
- 초점: **파괴적 동작**(리소스 삭제/공개 전환/이미지 롤백/잠금),
  **오타겟**(다른 env·release·state에 쓰기), **시크릿 누출**,
  **신규 설치↔업데이트 경로 불일치**, **캡처/채택 정확성**.
- 매 라운드 수정은 `deploy/tests/` 회귀 테스트로 고정 —
  배포 도구 **478 tests** + 컴포넌트 스위트(admin-api·proxy·workers·
  installer·admin-ui) 통과. 컴포넌트 수정분 자체도 2인 독립 diff 회귀검증
  (OTEL bearer 계층 이동의 stale-잔존·disable 계약·v1 드리프트·비원자
  캐시쓰기 등 9건 수정)을 거쳤다.

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
| 24-25차 | e7f5db28 + 66b414e6 | 3인 리뷰 재투입 — ①websearch/chatagent 프로비저너(dead/deleting 상태·find_runtime 레이스·env/image drift·ambient IMAGE_TAG 스크럽) ②전 라이프사이클(merge_env prefix 와일드카드가 SMTP_USERNAME/OIDC_EMAIL_CLAIM 운영자키 삭제·tfvars_clean `<<-` 들여쓰기 종결자·`.match`+`$` 후행개행 우회→fullmatch·compose/Caddyfile 검증-전-쓰기·teardown ctx 미해석 fail-open·eks feature-off 증거게이트·release/namespace 변경 구release 고아화·upsert_env_file 원자화·chat_name 프리패치 게이트·인용 heredoc 구분자·azs 상한/무효CIDR) ③신규 diff 회귀검증+프로비저너 재검증(schema `str(None)`→`"None"` 오염→_opt_str·sizing 키 sorted TypeError·Route53 외부 ALB 레코드 삭제→소유검증·UPSERT 전 기존레코드 경고·moved/import 플랜 스코프우회·helm --cleanup-on-fail 버전게이트·wait_services PRIMARY만·extra-vars 원자화·pending-deletion project 파생·fullnameOverride·persist 0644 창→0600·ECR immutable skip/레이스·safeguards 스크립트 https 강제·비JSON 200·CRLF SSE) — **464 tests** |
| 26차 | a68916e0 + e7a914dd | 최종 수렴 스윕 2인 — ①잔여영역(doctor 비-dict 리프 AttributeError→리포트 전체 사망·비인용 eks_cluster_version·autoscaling 레이어 TypeError·destroy 후 stale alerts-dims 재적용→죽은 ALB 알람·upsert_env_file umask 창·capture 사이드카 폴백·ecs CHAT_AGENT_REGION 미검증) ②24-25차 diff 회귀검증(persist fchmod 누락+encoding·safeguards localhost 접두사 우회→urlparse·구release 가드 helm 오류 fail-open→not-found 매칭·Route53 외부리전 ALB 삭제→DNS 리전 파싱·images.tags 'None' 코어션→태그검증·ecs human chat_agent 가드 누락) — **465 tests** |
| 27차 | 4484f114 | 3인 독립 리뷰(보안 적대검토·프로비저너 심층·diff 회귀) — ①소유 증명 전면 교체: instance 라벨 위조로 공격자 ingress/deploy 채택 가능→`helm get manifest` 기록 기반 `_release_kind_names` 로 `_live_deploy_env`·ALB hostname·ingress host·ALB dims 전 경로 ②프로비저너 수명주기: teardown 실패 삼킴→추적키 소실→유료리소스 고아화(rc≠0+키보존)·pricing/websearch 외부 role 채택·trust 덮어쓰기·삭제→소유마커·interpreter 재생성 전파레이스·env drift 키셋·비READY update·래퍼 타임아웃 ③시크릿 채널: create-secret/Cognito 비밀번호 argv→`--cli-input-json file://`(0600)·`--plan` Secret data/stringData 평문출력→마스킹·`_atomic_write` umask 창→os.open(0600)+fchmod ④tfstate: 미소유 버킷 수렴 잠금→소유태그만·lock table 스키마 검증 ⑤`"not found"` 오매칭(context 단절을 release 부재로)→결합매칭·보존 URL/ARN 형식검증 후 `--set`·human chat_agent last-wins — **465 tests** |
| 28차 | 6408bc6e | 3인 독립 리뷰(27차 diff 회귀·크로스모듈/테스트 충실도·잔여영역) — ①**27차 자체 회귀**: `_MANIFEST_CACHE` 가 실패·stale 결과를 캐시해 fresh install 의 2-phase nextauthUrl·Route53 A레코드 3개·ALB dims·live env 보존 통째 스킵 → 실패 미캐시+upgrade 후 무효화 ②off-teardown 증거 소실: teardown 실패해도 upgrade 가 env 지워 유료리소스 재시도 불가 → `.pending-provisioner-teardown` 마커 + ecs 추적키 복원 ③캡처/doctor 라벨 위조 잔여(cidr/scheme/이미지/판정 주조) → capture `_release_manifest_names`·doctor deployment/Job 매니페스트 필터 ④helm values `environment` 경로탐색(`../../x`→외부 tfstate 읽기)·`_r53_alias_target` JMESPath 주입·`load_meta` chart/이름 검증·env_dir 이름일치 environments 루트 제한 ⑤plan env 평문 유출→민감 env value 마스킹+파싱불가 Secret 텍스트 마스킹 ⑥`"release"+"not found"` 오매칭(`context "release-eu" not found`)→정확 패턴 ⑦pricing status 오류-미배포 혼동·lock table RANGE 키·allowed_roles 콤마·ownerReferences[*]·dead interpreter·write_env_file 심링크·fd 누수 — **476 tests** |
| 29차 | 81c03a92 | 2인 독립 리뷰(28차 diff 회귀검증+신규 경로 심층) — ①**추적키 조기 저장**: ecs 가 teardown 전에 pop+저장해 중간 abort(SystemExit/ctrl-C)로 유료 리소스 추적 증거 영구 소실 → 수렴 후 단일 저장 + human-declared 를 pop 전에 평가(사람 소유 Runtime 의 ARN 주입 유지 — 지우면 /chat 영구 503) ②**module 실측 사멸**: managed/targeted apply 가 output 을 enable_chat_agent=false 로 재저장해 `module_alive`·eks `chat_agent_name` 신호 사멸 → init 후 apply 전 스냅샷(eks `pre_chat_name`/ecs `pre_module_name`) ③plan 마스킹 우회: NOTES 꼬리·파싱불가 비-Secret 문서·`value: |` 블록스칼라·플로우 env·인라인 `data:{}`·비-dict spec 크래시 → 섹션헤더 제거+전 문서 텍스트 마스킹+ephemeralContainers/List ④cross-kind 이름 충돌: capture 가 kind 병합 이름집합으로 우리 Deployment 동명 외부 Ingress 를 소유 인정 → (kind,name) 쌍 ⑤`release <word> not found` 오매칭(lock/name/revision)→인용·콜론 형식만 ⑥`allowed_roles` 와일드카드/ARN 영구 불일치(정확 `in` 비교)→admin-api fnmatch 매칭 ⑦chatagent teardown 도출명이 custom project 무시→`_agent_name` env_dir 경유·uninstall 캐시 무효화·hook 문서 '+added' 거짓 diff·비문자 env TypeError·마커 쓰기 침묵→원자화+경고 — **478 tests** |
| 30차 | 81f8d8c6 + 7149956d + 1fab3a63 | `deploy upgrade eks` 신규 기능(kiro-cli claude-opus-5.5 외부 adversarial 리뷰) — ①차단: kubectl `--kube-context` 오인자(helm 플래그)→`--context`+클러스터 일치 검증, plan 미검증 `-auto-approve`→저장 plan 범위 검사(module.eks 밖·파괴 변경 차단), tfvars 부재 머신의 access_entries 리셋→중단 ②중요: 반쪽 홉 재개(애드온 실측 비교로 남은 수렴 이어감), `wait available` 오인→워크로드별 `rollout status`, tfvars 정규식 파괴→브레이스 균형 스캔+원자 쓰기+.bak, auto.tfvars 덮어쓰기→plan 비어있으면 중단, 애드온 다운그레이드 가드, insights 사전 점검, StatefulSet 포함 ③실환경 회귀: time_sleep 재생성 오탐→예외 처리, 1.34→1.35 실환경 완주 — **593 tests** |

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
| [eks-upgrade.md](eks-upgrade.md) | `deploy upgrade eks` — 홉 단위 클러스터/애드온 업그레이드 | 수렴 |
| [features.md](features.md) | 기능 플래그 + boto3 프로비저너(web_search/bi_insight/pricing_lambda) | 수렴 |
| [sizing-capacity.md](sizing-capacity.md) | size_tier/capacity_tier/sizing·DB/Redis 예산 | 수렴 |
| [ecs-compose.md](ecs-compose.md) | ECS(배포 보류)·compose 로컬 경로 | 조건부 수렴 |
| [admin-api.md](admin-api.md) | 관리 API — 인증/인가·VK 라이프사이클·예산·STS 로그인 | 수렴(HIGH 1·MED 1 수정) |
| [gateway-proxy.md](gateway-proxy.md) | 게이트웨이 프록시 — VK/JWT·인가·레이트리밋·예산·회계 | 조건부 수렴(MED 1·LOW 1 + diff 스윕 1건 수정, MED 1 문서화) |
| [workers.md](workers.md) | cost-recorder·notification·pricing-lambda·policy-helper | 수렴(MED 3 등 7건 + diff 스윕 2건 수정) |
| [clients-agents.md](clients-agents.md) | gateway-cli·installer·cowork-installer·admin-chat-agent·query_db | 조건부 수렴(MED 1·LOW 3 + diff 스윕 9건 수정) |
| [admin-ui.md](admin-ui.md) | Next.js 관리자 콘솔 — OIDC/PKCE·BFF 프록시·CSRF·시크릿 처리 | 수렴(MED 1·LOW 3 수정) |
| [app-maintenance-manual.md](app-maintenance-manual.md) | **애플리케이션 정비 매뉴얼(AMM)** — 5개 레포트를 CAR/FT/DD 체계로 통합 | 지상검사 완료·비행시험 대기 |
| [pr4-web-sso-provision.md](pr4-web-sso-provision.md) | **PR #4 머지 기록** — /v1/auth/provision JIT 등록·마법사 기본값·compose OIDC env + 충돌 해소 | 머지 완료(`1ab19b29`) |

\* 캡처는 라이브 AWS/K8s 에서만 최종 검증 가능 — 코드 경로는 수렴.

## 공통 잔여 리스크

- **라이브 검증 미수행**: 모든 수정은 단위 테스트 + 코드 추적으로 검증됐다.
  실제 EKS 클러스터/AWS 계정에서의 end-to-end(bootstrap → apply →
  capture → teardown) 실행 검증은 별도 필요.
- **AWS API 페이지네이션**: 조회성 호출(`describe-*`)은 페이지네이션이
  없다 — 리소스가 수백 단위로 많은 계정에서 누락 가능(LOW).
- **희귀 YAML 형태**: sanitize/parser 가 커버하지 않는 극단적 YAML
  변형이 남아있을 수 있으나, 실패 방향은 전부 fail-closed.
