# 리뷰: 기존 배포 캡처→채택 (capture/adopt)

**범위**: `deploy/capture.py` 전체(capture_eks/capture_ecs/capture_compose,
_instance_items/_dmap/_tfvars/_tf_backend/_gen_root/dropped_keys/
diff_docs), `deploy/cli.py` 의 `cmd_capture`+interactive absorb.

**판정: 수렴*** — 코드 경로 수렴, 라이브 대상 검증만 잔여.

## 계약

- 라이브 상태(helm values + kubectl + terraform outputs + tfvars + gen
  managed overlay)를 역산해 `gateway.captured-<env>.yaml` 을 만든다.
- 읽지 못한 필드는 **기본값이 아니라 `unknown` 으로 표시** — 채택 시
  fail-closed 로 잠그되 선언값을 조용히 덮지 않는다.
- 채택 전 `dropped_keys` 가 "cp 하면 삭제되는 yaml 전용 키" 를 경고하고
  `--interactive` 병합 채택을 지원한다.

## 발견·수정된 결함

### HIGH

- **공유 namespace 외부 흡수** (18차): ingress/deployment 를 라벨 없이
  집어 다른 릴리스의 scheme/CIDR/이미지 태그가 캡처됨 →
  `app.kubernetes.io/instance` 필터 + 무라벨 폴백은 타 인스턴스 라벨
  명시 제외.
- **사이드카 이미지 오염** (18차): 모든 컨테이너 순회로 마지막 콜론 태그
  승자(otel 등)가 앱 태그로 기록 → 서비스명 컨테이너 우선 선택
  (ecs task-def env 도 동일).
- **ECS internal ALB 공개 전환** (18차): `network.mode="public"` 하드코딩
  으로 채택 apply 가 internal ALB 를 public 으로 재생성 →
  `_alb_ingress_cidrs` 가 scheme 반환해 `private` 매핑.
- **td 읽기 실패의 destructive false** (18차): task-def 조회 실패가
  body_logging 등을 false 로 기록 — `unknown` 표시로 무파괴 채택.
- **gen overlay 경로 오계산** (20차): `parents[3]/"gen"` 은
  `deployment/gen` 이 아니라 존재하지 않는 경로 — `alert_emails`/
  `infra.*` overlay 값이 조용히 유실돼 채택 apply 가 SNS 구독 전량
  삭제/infra 선언 수렴 → `_gen_root()` 정정.

### MEDIUM

- **보안 키 복원** (18차): `allowedAwsAccounts`·allowed_roles·bedrock
  모델 ARN·mantle_regions·cognito_suffix/oidc 필드를 tfvars/outputs 에서
  복원 — 못 읽으면 채택이 잠금/권한 축소를 만든다.
- **registry 보존** + 서비스별 `images.tags` 맵 + 태그 불일치 노트
  (gateway-proxy 대표, ecs 는 단일 tag 제약 명시).
- **migration appVersion 폴백 제외** — 독립 핀 유지.
- **scalable-targets min/max 캡처** (18차): 수동으로 올린 ECS autoscaling
  상한이 desired×factor 재계산으로 내려가던 유실 방지.
- **backend key/env 불일치 경고** (20차): 공유 env dir 의 `.terraform`
  캐시가 다른 env 를 가리키면 outputs 전체가 다른 env 값 — backend key
  `ecs/<env>/terraform.tfstate` 와 대조.
- **tf_env_dir 검증**: 캡처된 클러스터명 vs live kube context 대조.
- **cmd_capture**: 비-mapping yaml 가드, env 파일명 스키마 형식 제한
  (traversal 차단), 흡수 후 0600, 캡처본 다수 선택 안내.

## 검증

`test_capture.py` 54 tests — 인스턴스 필터·사이드카·ALB scheme·unknown·
dropped_keys·gen overlay·backend 불일치·소스 선택·스칼라 잎.

## 21차 검증 라운드 (독립 리뷰어 — 추가 수정)

- **compose 읽기실패 → "읽은 값" 오인** (F1): Caddyfile 부재 시
  `allowed_cidrs=[]`·`domain.mode=none` 이 실측으로 기록돼 채택 diff 가
  방화벽 삭제/도메인 해제 행으로 보임 → unknown 표기. `.env` 부재 시
  env 기반 키(provider·기능플래그·oidc·region) 전부 unknown.
  `cmd_apply` 가 `cfg.warnings()` 를 출력하지 않던 것도 함께 수정.
- **eks env_dir 미검증 채택** (F3): `terraform output` 실패 시 env_dir
  유지 → 같은 dir 의 backend/tfvars 가 캡처본에 탑재 → CaptureError 면
  env_dir 무효화(외부 state 의 tfstate/infra 유입 차단).
- **ecs ALB scheme 탐침 경로** (F4): domain+zone 둘 다 있어야만 탐침해
  domain-less internal ALB 가 public 기록으로 채택돼 인터넷 노출 →
  `alb_dns_name` output 폴백 + 검증 불가 시 `network.mode` unknown.
- **ecs domain 게이트** (F5): `https_enabled` 조건 때문에 인증서 검증
  대기 중인 도메인이 `mode=none` 기록 → 채택 apply 가 도메인 해제 →
  `admin.<domain>` 매치만으로 route53-acm 기록 + 미구성 노트.
- **`internal_api` provider 강등** (LOW-MED): 유효 스키마값인데 `mock`+
  `"스키마에 없는 값"` 오인 — compose/ecs 양쪽에서 `internal_api_url`
  (EMAIL_API_URL)·`internal_api_from` 매핑으로 수정.
- `service_names` output 비-dict 크래시 가드.

## 잔여 리스크

- AWS API 페이지네이션(`describe-*`) 없음 — 리소스 규모가 큰 계정에서
  목록 누락 가능(LOW).
- 캡처된 doc 이 스키마 무효(desired_count=0 등)면 `schema.from_dict`
  검증이 실행되어 미완성 필드로 표면 — 사람이 채우는 계약.

## 28차 적용 (캡처 신뢰 경계)

- **instance 라벨 위조 채택**(MEDIUM): 위조 ingress/deployment 의
  cidr/scheme/host/image-tag 가 캡처돼 채택본이 공격자 네트워크 상태로
  배포 — `helm get manifest` 기록 이름만 소유로 인정(`_release_manifest_names`)
  + 조회 불가 시 라벨 폴백(레거시 채택 계약 유지).
- **helm values `environment` 경로 탐색**(MEDIUM): 라이브 values 의 env 가
  `_auto_env_dir`/`gen` 경로 결합으로 `../../x` 외부 디렉터리를 읽어 tfstate/
  backend 정보를 캡처에 주입 — `_ENV_RE` 검증 후에만 경로 사용.
- **`_r53_alias_target` JMESPath 주입**(LOW): tfvars 유래 host 의 `'`/메타문자가
  `--query` 를 주조 — `_HOSTNAME_RE` 통과분만 보간.
- **cmd_capture 기본 release/namespace**(LOW-MED): 채택 yaml 의
  deploy.release/namespace 를 우선 — 기본 "llm-gateway" 오질의 해소.

## 29차 적용 (cross-kind 소유 증명)

- **kind 병합 이름 집합 충돌**(MEDIUM): `_release_manifest_names` 가 전
  kind 의 이름을 한 집합으로 병합해, 우리 Deployment 와 동명인 외부
  Ingress 가 소유로 통과해 cidr/scheme/host 주조 가능 — (kind,name) 쌍
  집합 + `_manifest_owned(kind)` per-kind 필터(Ingress/Deployment).
- **빈 owned 집합의 strict 처리**(LOW): 매니페스트 조회 성공+해당 kind 0건
  일 때 strict-공집합으로 전부 누락 — doctor 와 같은 truthy 계약(빈 집합은
  라벨 폴백)으로 정합.

