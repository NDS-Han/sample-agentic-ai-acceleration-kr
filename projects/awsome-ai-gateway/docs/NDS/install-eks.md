# EKS — 기존 배포 온보딩 및 업데이트

대상: 이미 `install-eks.sh` + helm + terraform 으로 배포된 EKS 환경(dev/prod).
이 경로는 **기존 배포를 이어받아** helm 릴리스만 관리합니다 — EKS 클러스터·Aurora·
ElastiCache·Cognito·IRSA·ESO 같은 인프라는 기존 terraform env 가 계속 소유합니다.

## 사전 조건

- `kubectl` 로 클러스터 접근 (`kubectl get ns llm-gateway` 가 되는 context)
- `helm` ≥ 3.14
- (선택) terraform state 읽기 권한 — 있으면 DB/Redis/IRSA/cognito 값을 자동 주입.
  없으면 helm 만 업데이트하고 동적값은 기존 릴리스 값을 그대로 씁니다

## 1. 온보딩 — 현재 배포를 gateway.yaml 로

```bash
./deploy doctor --capture --target eks \
  --namespace llm-gateway --release llm-gateway \
  --env-dir deployment/terraform/environments/llm-gateway-dev
# → deployment/gateway.captured-dev.yaml

vi deployment/gateway.captured-dev.yaml   # notes 의 빈칸 채우기
#   (대화형이 나으면: ./deploy configure --config deployment/gateway.captured-dev.yaml --no-apply)
./deploy validate --config deployment/gateway.captured-dev.yaml
mv deployment/gateway.captured-dev.yaml deployment/gateway.yaml
```

capture notes 가 알려주는 주의점:

- **서비스별 이미지 태그가 다른 경우**(예: proxy 1.0.86 / api 1.0.89 / ui 1.0.168) —
  `images.tag` 는 하나뿐이라, 첫 `apply` 에서 **전 서비스가 같은 태그로 통일**됩니다.
  의도된 통일이면 그대로, 아니면 태그를 먼저 정리하세요.
- `domain.name` 은 `gateway.<base>`/`gateway-dev.<base>` 에서 base 만 추출합니다.
  다음 `render` 가 `gateway./admin./admin-api.<name>` 네이밍을 씁니다 — 기존
  `gateway-dev.*` 패턴과 다르면 URL 이 바뀌니 확인하세요.

## 2. 업데이트 — 변경 반영

```bash
./deploy configure   # 현재 설정을 기본값으로 항목별 확인 → 끝에서 "지금 배포?" y
                     # → render → plan → 확인 → apply 까지 자동 연결
```

또는 파일 직접 편집 경로:

```bash
vi deployment/gateway.yaml     # 예: images.tag 를 새 버전으로
./deploy validate
./deploy render                # → gen/<env>/eks/values.yaml + deploy.yaml
./deploy apply --plan          # helm upgrade --dry-run (변경 없음)
./deploy apply                 # plan 출력 → 확인(y) → helm upgrade --install
```

### 내부 동작

1. values 레이어 = `values.yaml`(차트 기본) → `values-eks-fargate-<env>.yaml`(기존)
   → `gen/<env>/eks/values.yaml`(우리 오버레이, 마지막이 이긴다)
2. `--set` 동적값 = terraform output 에서 해석 (install-eks.sh 와 동일 계약):
   `global.imageRegistry`, `database.external.host`, `redis.external.host`,
   `*.serviceAccount.annotations.eks.amazonaws.com/role-arn`, cognito issuer,
   body-logging env
3. migration = 차트의 pre-upgrade hook Job — 자동 실행, 실패 시 upgrade 실패
4. helm3 `--atomic` / helm4 `--rollback-on-failure` + `--cleanup-on-fail` 자동 선택
5. domain=none 이면 live ingress 의 ALB hostname 을 `adminUi.nextauthUrl` 로 2차 주입

> **`gen/<env>/eks/values.yaml` 은 생성물입니다 — 직접 고치지 마세요.**
> 기존 `values-eks-fargate-<env>.yaml` 도 그대로 둡니다 — 우리 오버레이가
> 마지막 레이어라서 필요한 키만 덮어씁니다.

## 3. 상태 확인

```bash
./deploy doctor    # release 상태, deployment readiness, migration job,
                   # 관리 키 drift, terraform plan drift
```

## 이 경로가 관리하는 것 / 하지 않는 것

| 관리함 (gateway.yaml → helm) | 관리하지 않음 (기존 terraform/스크립트 소유) |
|---|---|
| 이미지 태그(전 서비스 통일) | EKS 클러스터·Fargate profile |
| ingress host·inbound-cidrs·scheme | Aurora·ElastiCache·VPC |
| OIDC 계약(3개 서비스 동일) | Cognito user pool 자체 |
| notification provider·발신자 | IRSA role 생성(ARN 주입만) |
| WEB_SEARCH/body_logging 플래그 | AgentCore Gateway·Firehose 생성 |
| migration hook 실행 | ESO·SecretStore |

인프라 변경이 필요하면 해당 terraform env(`environments/llm-gateway-<env>`)에서
`terraform plan/apply` — doctor 가 그쪽 drift 도 같이 봅니다.
