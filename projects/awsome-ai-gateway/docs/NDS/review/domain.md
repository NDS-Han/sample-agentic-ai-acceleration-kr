# 리뷰: 도메인 부착 (Route53/ACM/selfsigned-temp)

**범위**: `deploy/eks_apply.py` 의 `ensure_domain_cert`/`_find_domain_cert`/
`ensure_domain_records`/`ensure_selfsigned_cert`/`live_alb_hostname`/
`_alb_hostname_by_suffix`/`live_ingress_rule_host`, `deploy/schema.py` 의
`domain.*`, `deploy/render/eks.py` 의 ingress 주석 합성.

**판정: 수렴**

## 모드 계약

- `route53-acm`: ACM 인증서(DNS 검증 CNAME 자동 등록) + 3개 호스트
  (`gateway.`/`admin.`/`admin-api.`) A-레코드를 ALB alias 로 UPSERT.
- `selfsigned-temp`: 도메인 없이 임시 https — 브라우저 경고, EKS 전용.
- `none`: HTTP only — Cowork/OIDC 콜백 불가.
- `network.admin_internal`: admin-ui/admin-api ingress 만 internal ALB.

## 발견·수정된 결함

### CRITICAL/HIGH

- **3개 ingress 라벨 fall-through** (13차): 차트가 `app.kubernetes.io/
  instance` 라벨을 3개 ingress 전부에 찍는데, 라벨 일치 시 이름 검사를
  건너뛰어 kubectl 이름순 첫 항목(`-admin-api`)이 항상 선택 →
  `nextauthUrl`·Route53 3개 레코드 전부가 admin-api ALB 를 가리키던
  회귀 → 소유 확인 후에도 이름 구분 항상 적용(fullnameOverride 는
  endswith, 무라벨 수동 ingress 는 정확 매치).
- **selfsigned 데드락** (16차): cert 를 ALB 존재에 묶어 첫 설치 불가.
- **cert 재사용 상태 미검증** (16차): EXPIRED/비활성 cert 를 ARN 만으로
  재사용 → Status 확인.
- **stale R53 레코드 정리** (16차): 선언 목록에 없는 파생 기본명만,
  ALB alias 여부 확인 후 삭제(수동 레코드 보호·커스텀 호스트는 경고만).
- **zone_id wizard 소실** (19차): eks 도 Route53 자동화에 필요 — ecs
  전용 질문이라 eks 재configure 시 `""` 로 덮여 자동화 죽음.

### MEDIUM

- **admin_internal 드롭** (16차): per-ingress dict 통째 교체로 scheme
  어노테이션 유실 — internal 선언이 공개 ALB 로 뒤집히던 경로.
- **_find_domain_cert 우선순위**: 첫 매치 반환이라 ISSUED 가 뒤면 죽은
  cert 집음 → ISSUED→PENDING 우선 정렬 + wildcard SAN 요구.
- **_alb_ingress_cidrs**: 라이브 SG rule 에서 CIDR 추출 — 라이브 제한이
  stale helm values 보다 우선(공개 역전 방지).
- **ACM DNS 검증 CNAME 자동 등록**: zone_id 없으면 수동 등록 안내로
  폴백(자동화 중단이 아닌 안내).

## 검증

`test_eks_apply.py` — 라벨 보유 ingress 선택·suffix 매칭·cert 우선순위·
stale 레코드 ALB 조건·`test_capture.py` wizard zone_id 보존.

## 23차 적용 (독립 리뷰 — 추가 수정)

- **scheme 을 mode 에서 도출**: `domain.name` + `mode!=route53-acm` 시
  nextauthUrl·client-values·Cognito 콜백이 `https://` 를 만들어 HTTP 전용
  리스너에 죽은 주소를 안내했다. mode=none→http, selfsigned-temp 의
  gateway/admin-api→http(adminUi 전용 TLS), Cognito 콜백은 mode=none 에
  미방출(https 필수라 등록 거절). schema 경고 추가(도메인 선언+비-acm
  mode 조합).
- **selfsigned-temp ingress 핀**: enabled/className 게이트 밖이면 차트
  기본 className=nginx 로 ALB 미생성 — 분기에서 `enabled/className=alb`
  보장.
- **client-values 폴백**: ecs mode=none/cloudfront-temp·compose mode=none
  은 도메인 DNS 미생성이라 svc.domain 안내 대신 ALB DNS/공인 IP 폴백.

## 잔여 리스크

- Route53 API 호출은 페이지네이션 없음 — 레코드 수가 매우 많은 zone 에서
  목록 누락 가능(LOW).
- selfsigned-temp 는 브라우저 경고가 있는 임시 모드 — prod 사용은
  스키마 경고 대상.
