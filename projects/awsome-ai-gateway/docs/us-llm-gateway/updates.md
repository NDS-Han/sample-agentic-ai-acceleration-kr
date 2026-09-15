# 업데이트 이력 (US-NN)

**한국어** · [English](updates.en.md)

README 의 「최신 업데이트」는 최근 5개만 보여준다 — 여기가 전체 이력이다. `US-NN` 은 리베이스에 영향받지 않는 고정 ID. 적용 상태는 배포 EC2 에서 `bash status.sh`.
등급 — **필수**: 반드시 · **권장**: 안 하면 그 기능 동작 안 함 · **선택**: 요구 있을 때

| ID (문서) | 무엇 | 등급 · 신규 설치 | 기존 배포가 할 일 |
|---|---|---|---|
| [**US-11**](update-scripts/README.md#단가-갱신-08) 2026/09 | 모델 단가 정정 — `us.` 지리 CRIS 는 Standard 티어(Global ×1.1) · Sonnet 5 9/1 인상 취소 반영 | 필수(청구 정합) · 신규 설치는 불필요(§4-2 (C) 가 Standard 단가를 심음) | US-10 배포 뒤 — 8-D ⑧ 에 포함(`08 --apply`, 5분 캐시) · alias 가 global.* 이면 `02 --remap` |
| [**US-10**](ops/8-D-upstream-sync.md) 2026/09 | upstream 동기화 배포 — phase-2 이식(마이그레이션 0026→0036 · 이미지 6종) + 9/14~15 결함 수정: readiness 전면 503 · thinking 400 · web search 루프 · 예산 이중 청구 | 필수(가용성 결함 수정) · 신규 설치는 포함(US-01 이 이 코드로 설치) | [8-D](ops/8-D-upstream-sync.md) ①~⑨: 사전 점검 → DB 스냅샷 → `13` 태그 → 이미지 6개 → `install-eks.sh` → `08` 단가 → `14` 점검 |
| [**US-09**](cowork/installer/cowork-installer-admin-e2e-windows.md) 2026/08 | Cowork Windows 설치기 — 관리자가 .exe 1개 빌드 → 직원 PC 설치(HKLM 정책) | 선택 · Cowork Windows 쓰면 권장(수동 설치 대체) · 게이트웨이 변경 없음 | 빌드 PC 에서 `feat/cowork-installer-import` clone → `07-client-values.sh` 값으로 `site-config.json` → `build.ps1` → 직원 PC 설치 + `setup` |
| [**US-08**](ops/8-P-prod.md) 2026/08 | prod 스택 신설 — 별도 계정 · https + admin internal + VPN · Cowork Windows | 선택 · POC 이후 운영 전환 시 · `environment=prod` | dev 는 그대로 두고 prod 계정에 §1~§6 재실행(8-P 순서) |
| [**US-07**](ops/8-I-admin-internal.md) 2026/08 | 고객사 최종 아키텍처 — admin ALB 2개를 internal 로 | 선택 · 전제 S2S VPN · POC 신규는 §3-6 시점에 values 주석 해제 · 운영(`US-08`)은 포함 | values 주석 2곳 해제 → helm(ALB 재생성) → admin SG·CNAME 교체 |
| [**US-06**](ops/8-H-alb-https.md) 2026/08 | ALB HTTPS — 커스텀 도메인 + ACM 인증서 | 선택 · POC 는 도메인 있을 때 · 운영(`US-08`)은 포함 | 도메인 확보 → 전환 → 클라이언트 URL 2개 교체 (약 30분) |
| [**US-05**](ops/8-E-eks-upgrade.md) 2026/08 | EKS 1.31 → 1.34 | 필수(지원 만료·비용) · 신규 포함 | 1단계씩 3회 apply + 전 ns 파드 재시작 |
| [**US-04**](ops/8-N-vpc-endpoint.md) 2026/08 | Bedrock·STS 를 NAT 대신 VPC Endpoint 로 | 필수(컴플라이언스) · 신규 포함 | 엔드포인트 apply → gateway-proxy 재시작 |
| [**US-03**](ops/8-U-update.md) 2026/08 | Admin UI 한/영 토글 | 필수(영문 지원) · 신규 포함 | admin-ui 이미지 재빌드 → install-eks |
| [**US-02**](update-scripts/README.md#실행-순서) 2026/08 | Cowork 연결 + Opus 5 등록 | 항목별 — Cowork 쓰면 `01`·`03`, Opus 5 쓰면 `02` 필수 · 🔴 **신규도 해당** | 01 라우팅 · 02 모델(단가 필수) · 03 CloudFront(도메인 없을 때만) |
| [**US-01**](install-overview.md) 2026/07 | 최초 설치 (기준선) | — | — |

## 왜 · 함정 (항목별)

- **US-11** — 게이트웨이 단가는 AWS 청구와 같아야 팀 예산·ROI 가 맞는다. `us.anthropic.*`(지리 CRIS)는 Price List 의 **Standard 티어**(Global ×1.1)로 청구된다(891 Cost Explorer 실측 /1M: Opus 5 $5.50/$27.50 · Sonnet 5 $2.20/$11 · Haiku 4.5 $1.10/$5.50). 종전 행은 Global 단가라 Opus 5 가 10% 과소였고, Sonnet 5 는 출시 때 예고된 9/1 인상($3/$15)이 **취소**됐는데 미리 반영돼 36% 과대였다. 정본은 `update-scripts/pricing.tsv`, 적용은 `08`(열린 행을 닫고 새 행, `effective_from` = 적용 시각 — 과거 `usage_logs` 는 재계산되지 않음). 함정: Redis `model:{alias}` 캐시 300초라 5분 뒤 반영, 파드 재시작은 무의미. upstream 마이그레이션 0030 이 넣는 Sonnet 5 $2/$10 은 Global 값이라 upstream 동기화 뒤에도 `08` 을 한 번 더 돌린다.
- **US-10** — upstream 이 phase-2 벤더 구현 전체(앱별 모델 허용목록 0035 · `system_settings` 0036 · 본문 로깅(3겹 스위치 전부 OFF) · 유니크 인덱스 0034 · 설치기 · UI 번역)와 9/14~15 결함 수정 13커밋을 들여왔다. US 에 직접 닿는 수정 = `/health/ready` 가 Redis 저하에도 503 을 내 전 파드가 ALB 에서 동시에 빠지던 것 · Claude Code 가 thinking `enabled` 를 보내면 전부 400 · web search 루프 6건(마지막 턴 400 · `tool_use` 누출로 재전송 · 검색 수 무제한) · cost-recorder 이중 청구/PEL 유실 · 예산 DB 폴백이 정책 무시. 함정: ① 같은 태그로 rebuild 하면 helm rollback 불가 → `13` 으로 먼저 태그 ② migration 이 롤아웃보다 먼저 돌아 새 파드 Ready 까지 옛 파드의 모델 목록 조회가 수 분 실패할 수 있음 — 그 창에서 rollback 금지 ③ 0027 이 `global.anthropic.*` full-ID alias 를 ACTIVE 로 심음 → admin UI 에서 INACTIVE(`14` 가 알려줌) ④ helm rollback 은 스키마를 안 되돌림 → DB 스냅샷 먼저(8-D ③) ⑤ terraform 은 plan 만(apply 불필요 · ESO 웹훅 재활성은 별도 결정).
- **US-09** — Cowork Windows 설치기: 수동 가이드(레지스트리 6키 손입력) 대신 관리자가 **설치 파일 1개**를 빌드해 배포한다 — 빌드 PC(`site-config.json` = `07-client-values.sh` 출력) → `gateway-cli-cowork-setup-<ver>.exe` → 직원 PC 에서 설치 + `gateway-cli-cowork setup`(HKLM `Policies\Claude` inference* 6키, 머신 전역) → 사용자는 Claude Desktop(offline .msix) + `login`. 게이트웨이·차트 변경 없음. 함정: HKLM 정책이라 한 PC 에 dev·prod 공존 불가 · Cowork 샌드박스는 Hyper-V 필요(EC2 면 metal + `VirtualMachinePlatform`·`Containers`) · `winget` 은 `--source winget` 명시 · 코드는 `feat/cowork-installer-import` 브랜치. 순서: [관리자 E2E](cowork/installer/cowork-installer-admin-e2e-windows.md) → [빌드](cowork/installer/cowork-installer-build-windows.md) · [사용자](cowork/installer/cowork-installer-user-windows.md) · [제거](cowork/installer/cowork-installer-uninstall-windows.md).
- **US-08** — prod 승격: dev 를 바꾸는 게 아니라 **별도 계정에 prod 스택을 새로** 세운다(`environment = "prod"` 한 줄 = HA 사이징). 처음부터 https(US-06)+admin internal(US-07) 로 세우므로 **VPN 이 먼저** — 없으면 VK 발급이 막힌다(검증은 AWS Client VPN 으로 대체). 함정: 네트워크 CIDR 은 dev 와 겹치면 안 됨(VPN 라우팅) · prod values 이미지 태그·`elasticache_endpoint`(cluster mode) 는 2026-08 수정본 필요 · Cowork Windows 테스트 머신은 metal(Hyper-V). 상세 [8-P §5](ops/8-P-prod.md#5-함정-모음-검증-계정-실측).
- **US-07** — 고객사 최종형: 컨트롤 플레인(admin-api·admin-ui) ALB 를 private 서브넷 internal 로 내려 S2S VPN 으로만 접근(데이터 플레인 gateway 는 public 유지). terraform 무변경 — vpc 모듈이 서브넷·태그를 이미 만든다. 신규 설치는 `US-01` 의 §3-6 시점에 values 주석 해제로 처음부터 internal(별도 절차 없음, install-guide §3-6 안내 참조), 운영 중 배포는 [§8-I 전환 절차](ops/8-I-admin-internal.md) — ALB 재생성이라 admin CNAME 교체 + 수 분 단절. ⚠️ S2S VPN 없이 적용하면 VK 발급(api-key-helper → admin-api)이 끊겨 게이트웨이 사용 자체가 불가 — VPN 개통 전 적용 금지. internal ALB 생성·in-VPC 통신은 리허설로 실증(2026-08-20).
- **US-06** — ALB 3개를 http:80(임시 ALB 주소) 대신 `https://gateway-<env>.<도메인>` 으로. ACM TLS 종료·고정 이름, Cowork 용 CloudFront 불필요. 등록이 막힌 계정(Amazon 내부 등)은 타 계정 등록 + NS 위임. 적용 후 `ANTHROPIC_BASE_URL`·`ADMIN_API_URL` 교체.
- **US-05** — 1.31 은 표준 지원 종료로 연장 요금(클러스터당 월 ~$365) · 최종 종료(2026-11-26) 후 강제 자동 업그레이드. 마이너 1단계씩만(3회 apply), 단계마다 전 ns 파드 재시작(Fargate 는 파드=노드).
- **US-04** — Bedrock·STS 호출이 NAT·퍼블릭 인터넷 대신 VPC 내부 PrivateLink 로. 엔드포인트 선언이 들어가기 전에 만든 VPC 만 대상(신규는 이미 포함) — Bedrock 은 계속 성공하니 아무도 안 알려준다. 적용 직후 gateway-proxy 재시작 필수(풀에 남은 죽은 소켓 → 연속 502 를 엔드포인트 탓으로 오진).
- **US-03** — 관리 화면 i18n, 헤더 KO/EN 토글이 실제로 번역. admin-ui 이미지 재빌드가 필요.
- **US-02** — 설치 마이그레이션이 Cowork 라우팅 행을 존재하지 않는 계정으로 심어 그대로 두면 Cowork 전부 502(`01`). `02` 모델 등록은 Claude Code 에서 Opus 5 를 쓸 때도 필요(설치 시드엔 Opus 5 없음) — 단가를 빼먹으면 비용 `$0` 기록·예산 우회. `03` CloudFront 는 도메인 없이 Cowork https 를 만들 때만(US-06 이면 불필요). Claude Code 만 + 시드 모델이면 전체 생략 가능.
- **US-01** — 단일 계정 · us-west-2 · Claude Code · US Geo 추론 기준선.
