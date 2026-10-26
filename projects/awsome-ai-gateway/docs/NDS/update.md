# 업데이트 · 롤백

모든 변경은 `gateway.yaml` 하나가 원본이고 `./deploy`가 나머지를 합니다.
일상 경로는 **`./deploy configure`(항목별 확인 → 배포까지)**, 파일 직접 편집 경로는
**`./deploy apply`(render 내장)** 입니다 — 환경별 스크립트를 찾아 돌릴 필요가 없습니다.

## 기존 배포를 gateway.yaml 로 온보딩 (capture)

이미 배포된 환경(스크립트/수작업)을 선언적 관리로 가져옵니다:

```bash
# 1. 현재 배포 상태를 읽어 gateway.yaml 후보 생성
./deploy doctor --capture --gen-dir deployment/gen/<env>
#    → deployment/gateway.captured-<env>.yaml 생성 (기존 yaml은 덮지 않음)

# ecs 는 terraform state 를 읽으므로 AWS 자격이 필요합니다
./deploy doctor --capture --target ecs --region ap-northeast-2

# 2. 후보 파일 검토 — capture notes 가 못 읽은 값을 알려줍니다
vi deployment/gateway.captured-<env>.yaml

# 3. 검증 후 gateway.yaml 로 채택
./deploy validate --config deployment/gateway.captured-<env>.yaml
mv deployment/gateway.captured-<env>.yaml deployment/gateway.yaml
```

기존 `gateway.yaml` 이 있는 상태에서 capture 하면 **차이 테이블**이 나옵니다
(yaml 값 ←→ 배포된 실제 값). `--interactive` 를 붙이면 항목별로 선택합니다:

- **absorb** — 실제 값을 yaml 에 채택 (수동 변경을 정식 설정으로 흡수)
- **keep** — yaml 유지 (다음 `apply` 가 실제 상태를 yaml 대로 되돌림)
- **skip** — 지금은 건너뜀

> 캡처가 못 읽는 값: `.env` 의 시크릿, compose 의 `network.mode`, ecs 의
> `zone_id`/`allowed_cidrs`/`tfstate_*` — 파일 끝의 `# capture notes` 를
> 확인하고 직접 채우세요. ecs 의 DB/캐시 실제 토폴로지(ACU·node_type·노드/샤드
> 수)는 `infra.db`/`infra.cache` override 로 기록되므로 채택해도 apply 가
> 실제 인프라를 tier 프리셋으로 되돌려 축소하지 않습니다.

### 수작업 compose 배포 온보딩 — 데이터 이관 주의

`--gen-dir`이 `deployment/gen/<env>` 밖(예: 수작업으로 `docker compose up` 한
디렉토리)을 가리키면, 채택 후 `apply`는 **새 compose 프로젝트**(`llm-gateway-<env>`)와
**새 볼륨**으로 기동합니다 — 기존 DB 데이터와 `.env` 시크릿이 **이어지지 않습니다**.
같은 호스트에서 기존 스택이 떠 있으면 포트 충돌로 실패합니다.

데이터를 이어가려면 apply 전에:

```bash
# 1. 시크릿 보존 — 기존 .env 를 새 gen 디렉토리로 (render 가 기존 키를 보존)
./deploy render --config deployment/gateway.yaml   # gen/<env>/ 먼저 생성
cp <기존 디렉토리>/.env deployment/gen/<env>/.env

# 2. DB 데이터 이관 — 볼륨명이 프로젝트에 붙는다 (<old-project>_pgdata)
docker run --rm -v <old-project>_pgdata:/from -v llm-gateway-<env>_pgdata:/to \
  alpine sh -c 'cp -a /from/. /to/'
#   (기존 스택을 내린 뒤 실행 — 쓰기 중 복사는 손상될 수 있음)
```

그래도 `VIRTUAL_KEY_ENCRYPTION_KEY`가 바뀌면 발급된 Virtual Key는 전부 무효입니다 —
.env의 이 키만큼은 반드시 옮기세요.

**eks 캡처** — helm release 의 effective values + kubectl 라이브 상태를 읽습니다
(kubeconfig 에 클러스터 접근이 필요):

```bash
./deploy doctor --capture --target eks \
  [--namespace llm-gateway] [--release llm-gateway] [--context <ctx>] \
  [--env-dir deployment/terraform/environments/llm-gateway-<env>]
```

읽는 것: 리전, 도메인/CIDR(ingress 어노테이션), notification provider,
feature 플래그(WEB_SEARCH/FIREHOSE/otel), OIDC 계약, 서비스별 이미지 태그.
라이브 ingress/deployment 와 values 가 다르면(kubectl 패치·set image) notes 로
드리프트를 알립니다. 서비스별로 태그가 다른 것(단일 `images.tag`로 표현 불가),
ESO/Fargate/RDS Proxy 등 eks 고유 구성도 notes 에 기록됩니다 — 캡처본은
**온보딩용 후보**이지 eks 재배포 가능한 완전한 선언이 아닙니다.

### us-llm-gateway(레거시 스크립트) 배포를 온보딩할 때

레거시 배포도 EKS helm release 이므로 위 절차로 캡처됩니다 — 채택 후
`apply`는 차트 base → `values-eks-fargate-<env>[.local].yaml` → gen overlay →
`--set`(tf output) 순으로 쌓으므로 **env values 파일이 소유한 값(시크릿 경로,
ESO, Fargate, HPA/requests)은 그대로 유지**되고, terraform 인프라도 env
tfvars 소유로 남습니다. 다만 채택 전 두 가지를 결정하세요:

1. **Ingress host 네이밍** — 레거시는 `gateway-dev.<domain>` 형태, NDS 는
   `domain.name`이 있으면 `gateway./admin./admin-api.<domain>`으로 재작성합니다.
   - 이름을 바꿔도 되면(dev 등): 그대로 채택 → apply 후 Route53 레코드와
     클라이언트 URL, admin nextauth 를 새 host로 갱신. ACM 인증서가
     `*.<domain>` 와일드카드면 같은 레벨 이름이라 별도 발급 불필요.
   - 기존 이름을 유지해야 하면(prod 등): 채택본에서 `domain.mode: none`으로
     두고 `domain.name`을 비우세요 — overlay 가 host 를 건드리지 않아 env
     values 의 `gateway-dev.*` 와 인증서 어노테이션이 유지됩니다. yaml 상
     mode=none 이 실제와 다른 표현이라는 점만 감안하세요.
2. **첫 apply 는 차트 업그레이드를 겸합니다** — 로컬 repo 의 차트가 설치된
   release 보다 새 버전일 수 있습니다. `./deploy apply --plan` 으로 helm
   dry-run/차이를 먼저 보고, 가능하면 의도된 업그레이드 창에서 채택하세요.

채택 후 `./deploy doctor` 로 live 와의 drift 를 확인하고, env terraform 을
건드리는 경우 managed overlay(`gen/<env>/eks/terraform.tfvars`의
`enable_*` 키가 live 기준으로 기록됨)를 `-var-file`로 함께 넘기세요.

## 일반 업데이트 (기능 on/off, 설정 변경)

가장 쉬운 경로 — 현재 설정을 기본값으로 항목을 다시 물어보는 마법사:

```bash
./deploy configure     # 항목별 확인(Enter=유지) → 저장 → "지금 배포?" y
                       # → render → plan 미리보기 → 확인 → apply 까지 자동 연결
./deploy doctor        # ✓ 확인
```

파일을 직접 고치는 경로도 그대로 지원합니다:

```bash
vi deployment/gateway.yaml        # 변경
./deploy validate                 # 검증
./deploy apply --plan             # 변경 미리보기 (아무것도 안 바뀜)
./deploy apply                    # 확인 → 실제 적용
./deploy doctor                   # ✓ 확인
```

`doctor` 가 `.env` drift, 서비스 이상, 마이그레이션 head 불일치를 보고하면
해당 메시지대로 조치하세요.

> **eks 인프라 업그레이드는 `./deploy` 범위 밖입니다.** EKS 컨트롤플레인
> minor·애드온(coredns/kube-proxy/vpc-cni)·Aurora 엔진은 terraform env
> 소유라 사람이 tfvars를 편집해 `terraform apply`합니다 — 절차·버전 표·
> 롤백 제한은 [upgrade-eks-cluster.md](upgrade-eks-cluster.md). doctor가
> 선언↔라이브 버전 skew를 HIGH로 감지합니다.

## 코드 업데이트 (새 버전 배포)

backend 마다 **이미지를 누가 빌드하는지**가 다릅니다 — 이 차이가 절차를 결정합니다:

| backend | 이미지 빌드 | 태그 변경 위치 |
|---|---|---|
| compose | `docker compose up --build` 가 로컬 소스에서 빌드 | 불필요 (checkout된 소스가 곧 이미지) |
| ecs | `./deploy apply` 가 build+push 까지 수행 | `gateway.yaml` 의 `images.tag` |
| eks | **`./deploy apply` 는 빌드하지 않습니다** — helm 이 ECR 의 기존 태그를 참조할 뿐 | `gateway.yaml` 의 `images.tag`/`images.tags` + 사전에 `rebuild-image.sh` 로 push |

### compose

```bash
cd awsome-ai-gateway
git fetch && git checkout <새 브랜치/태그>
./deploy render
docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d --build
./deploy doctor
```

마이그레이션은 `migration` 서비스가 기동 시 자동으로 `alembic upgrade head`
까지 올립니다 — 앱보다 먼저 완료되도록 depends_on으로 순서가 보장됩니다.
`db/init/*.sql`(기본 조직·팀·service-token 시드)도 멱등 재적용되므로, 기존
볼륨에서도 업데이트에 추가된 시드가 반영됩니다.

### ecs

`images.tag` 를 새 태그로 바꾼 뒤 `apply` — task def 가 참조할 이미지를
apply 가 스스로 build/push 합니다:

```bash
git fetch && git checkout <새 브랜치/태그>
vi deployment/gateway.yaml     # images.tag: "<새 태그>"  (관례: git short SHA)
./deploy apply --plan
./deploy apply
./deploy doctor
```

### eks — 이미지 push → 태그 핀 → apply (3단계)

`./deploy apply` 는 helm upgrade 만 합니다. **태그만 바꾸면 ECR 에 그
이미지가 없어 새 파드가 ImagePullBackOff 로 멈춥니다** — 반드시 push 가
먼저입니다:

```bash
git fetch && git checkout <새 브랜치/태그>
TAG=$(git rev-parse --short HEAD)              # rebuild-image.sh 와 같은 태그 규약

# 1. 대상 계정/리전 확인 — 스크립트는 현재 AWS 자격증명의 ECR 에 push 한다
aws sts get-caller-identity --query Account

# 2. 이미지 빌드 + ECR push (DEPLOY=1 없이 — push 만)
export AWS_REGION=<대상 리전>
for s in gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker migration; do
  bash deployment/scripts/rebuild-image.sh "$s" <env>
done
#   scheduler 는 admin-api 와 이미지를 공유 — 별도 빌드 없음
#   이미 그 태그가 ECR 에 있으면 스크립트가 덮어쓰기를 거부 → 해당 서비스는 skip

# 3. gateway.yaml 에 태그 핀
vi deployment/gateway.yaml
#   images:
#     registry: <account>.dkr.ecr.<region>.amazonaws.com
#     tag: "<새 SHA>"              # 전 서비스 동일 태그면 여기만
#     tags:                        # 서비스별로 다르면 맵으로 (tag 를 개별 override)
#       gatewayProxy: "<SHA>"
#       migration: "<SHA>"         # migration 은 tags 맵에서만 핀 가능

# 4. 적용
./deploy validate
./deploy apply --plan            # 변경 미리보기
./deploy apply
./deploy doctor
```

`rebuild-image.sh` 참고:

- 태그는 **git HEAD short SHA** — 이미지 내용과 커밋이 1:1 로 대응하는
  불변 태그 규약입니다. 같은 태그 덮어쓰기는 스크립트가 거부합니다.
- `DEPLOY=1 bash rebuild-image.sh <svc> <env>` 는 push 후 helm upgrade 까지
  한 번에 하는 **빠른 패치 경로**입니다 — `gateway.yaml` 을 거치지 않아
  yaml↔live drift 가 생기므로, 정식 변경은 위 3단계 절차로 하고 DEPLOY=1 은
  핫픽스/디버깅에만 쓰세요 (다음 `./deploy apply` 가 yaml 값으로 되돌립니다).
- 서비스가 `migration` 이면 빌드 컨텍스트는 `db/` 입니다.

## 롤백

plane 별로 다릅니다 — "태그만 되돌리기"는 롤백이 아닙니다:

| 무엇이 바뀌었나 | 되돌리는 법 |
|---|---|
| 앱 코드/이미지 (compose) | 이전 커밋/태그로 `render` + `up -d` |
| 앱 코드/이미지 (eks) | `images.tag`/`images.tags` 를 이전 값으로 되돌려 `./deploy apply`. 실패한 upgrade 는 helm 이 자동 롤백(`--atomic`/`--rollback-on-failure`). 수동 리비전 복귀는 `helm rollback <release> -n <ns>` — 단 **migration hook 은 rollback 시 재실행되지 않는다** |
| 앱 코드/이미지 (ecs) | `images.tag` 를 이전 값으로 되돌려 `./deploy apply`. 배포 실패는 circuit breaker 가 서비스를 자동으로 이전 task def 로 되돌린다 |
| 설정(gateway.yaml) | 이전 값으로 되돌려 `render` + 재기동 |
| **DB 마이그레이션** | 다운그레이드 없음 — forward-fix (새 마이그레이션으로 수복) |
| `.env` 삭제/유실 | **백업에서 복원** — VIRTUAL_KEY_ENCRYPTION_KEY를 잃으면 발급된 Virtual Key 전부 재발급 필요 |

## 도메인을 나중에 얻었을 때

도메인 획득 절차(Route53 구매/NS 위임/사내 할당)와 backend 별 등록 순서의
전체 정리는 [domain.md](domain.md) — 여기는 변경 명령만 요약합니다.

```bash
# gateway.yaml
domain:
  mode: route53-acm       # compose는 caddy 가 자동 TLS
  name: example.com

./deploy render && docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d
```

DNS에 `gateway.`/`admin-api.`/`admin.` `example.com` A레코드(서버 IP)가 먼저
있어야 인증서가 발급됩니다. 기존 설치된 클라이언트는 게이트웨이 주소가 바뀌므로
새 주소를 다시 안내해야 합니다.

**ecs** — `domain.zone_id`(Route53 hosted zone)도 함께 채운 뒤 `./deploy apply`.
ACM 인증서는 모듈이 만들고 DNS 검증 레코드도 자동 생성됩니다. hosted zone 이
다른 계정에 있으면 `terraform output cert_validation_records` 로 레코드를
확인해 수동 등록 후 재적용하세요. 도메인 도입 시 NEXTAUTH_URL 도 새 도메인으로
자동 갱신됩니다 — 세션 재로그인이 필요합니다.

**eks** — cert 발급·`certificate-arn`·DNS CNAME 이 수동입니다. 절차는
[install-eks.md](install-eks.md) §4.

## public → private 전환

단일 플래그 전환이 아니라 **순서 있는 작업**입니다:

1. 사용자가 게이트웨이에 도달할 경로를 먼저 확보 (VPN/DX/사내망)
2. `network.allowed_cidrs` 를 사내 대역으로 좁히고 `render` + 재기동
3. Caddy 가 허용 대역 외 접속을 403으로 차단 (SG로도 이중 차단 권장)
4. `network.mode: private` 로 표시 후 인스턴스를 프라이빗 서브넷으로 이전 — 이 단계는 서버 재배치라 계획된 다운타임 필요

**ecs** — `network.mode: private` 은 ALB 를 internal 로 바꾸고 프라이빗
서브넷을 씁니다. 전환 전에 ① VPN/DX 등 사내망→VPC 경로, ② 도메인 사용 시
프라이빗 hosted zone 또는 사내 DNS 해석이 필요합니다. `render` → `apply` 가
ALB 를 교체하므로 짧은 다운타임이 생깁니다 — 유지보수 창에서 진행하세요.
private 에서도 NAT 는 유지됩니다(ECR pull·Bedrock 호출 경로).

## 배포 삭제 (teardown)

`./deploy teardown` — apply가 소유한 범위만 지웁니다. 확인은 y/n이 아니라
**환경 이름을 그대로 입력**해야 하고, CI에서는 `--yes`로 생략 가능합니다.

| backend | 지워지는 것 | 남는 것 |
|---|---|---|
| **compose** | 컨테이너 + 네트워크. `--purge` 시 볼륨까지 (**DB 데이터 영구 삭제**) | 볼륨(기본), `gen/<env>/` 산출물(.env 시크릿 포함), 빌드된 이미지 |
| **ecs** | terraform이 만든 전부 — VPC/Aurora/ElastiCache/ALB/ECS/Cognito/Secrets/ECR(repo+이미지) | `deployment/gen/` 산출물, terraform state backend(수동 관리) |
| **eks** | 기본: helm release(앱 전체)만. `--infra` 추가 시 terraform env 인프라(EKS·Aurora·VPC 등)까지 | 기본: 인프라 전부 보존 + ns 내 k8s secrets/pvc 잔여 가능. `--infra` 시: terraform state backend·Secrets 복구 유예기간·수동 생성 리소스 보존 |

모든 backend 에서 `web_search`를 켰다면 AgentCore WebSearch Gateway
(us-east-1) 도 함께 삭제됩니다 — terraform 관리 밖의 프로비저닝 리소스라
destroy 로는 지워지지 않아 CLI 가 별도로 정리합니다. `pricing_lambda`는
**ecs/eks만** 함께 삭제됩니다(apply 가 boto3로 만든 소유 리소스) — compose는
apply 가 만들지 않으므로(ops/NDS-01 수동 생성) teardown 도 건드리지 않고,
수동 생성했다면 `aws lambda delete-function`으로 직접 정리하세요.

```bash
./deploy teardown                    # 삭제 대상 미리보기 → 환경 이름 입력 → 삭제
./deploy teardown --purge            # compose: 볼륨(DB)까지 삭제
./deploy teardown --infra            # eks: helm 앱 + terraform env 인프라 전부 삭제
./deploy teardown --yes              # 비대화형 (CI) — 확인 생략, 주의
./deploy teardown --context <ctx>    # eks: 대상 클러스터 명시
```

주의:

- **ecs**: `db_safeguards`가 켜진 티어(t2 등)는 Aurora deletion protection이
  destroy를 막으므로, CLI가 해제 apply 후 destroy합니다. **최종 스냅샷 없이
  완전 삭제**됩니다 — 보존이 필요하면 먼저
  `aws rds create-db-cluster-snapshot`으로 수동 스냅샷을 만드세요.
- **eks**: 기본은 helm uninstall만 — 앱만 지웁니다. `--infra`를 붙이면
  `deploy.yaml`의 `env_dir`이 가리키는 terraform env에서
  `plan -destroy` 미리보기 → 확인 후 `destroy`로 **인프라 전체**(EKS 클러스터·
  Aurora·ElastiCache·Cognito·Secrets·VPC·IRSA·ALB controller)를 지웁니다.
  앱을 먼저 내린 뒤 인프라를 지우므로 ALB controller가 만든 리소스가 정리됩니다.
  - `environment=prod` 또는 provisioned DB는 Aurora deletion protection이
    destroy를 막습니다 — env 디렉토리의 설정으로 먼저 해제하세요.
  - 이미 helm release를 수동으로 지운 상태라도 `--infra`는 동작합니다
    (release가 없으면 helm 단계를 자동으로 건너뜁니다).
  - terraform state backend와 수동 생성 리소스는 지워지지 않습니다.
- **공통**: teardown은 `VIRTUAL_KEY_ENCRYPTION_KEY` 백업을 하지 않습니다 —
  삭제 전 `.env`(compose) 또는 Secrets Manager 값을 별도 보관하세요.
