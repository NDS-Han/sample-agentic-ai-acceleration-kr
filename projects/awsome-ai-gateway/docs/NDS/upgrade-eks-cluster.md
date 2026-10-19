# EKS 클러스터·의존성 업그레이드 런북

> **범위**: EKS 컨트롤플레인 minor · EKS 애드온(coredns/kube-proxy/vpc-cni) ·
> Fargate kubelet · Aurora 엔진 — **terraform env 소유**라 `./deploy apply`가
> 하지 않습니다(앱 업데이트는 [update.md](update.md)). 사람이 env tfvars를
> 편집하고 `terraform apply`로 올립니다.

> **환경 변수** — 맨 위 블록만 채우면 아래를 그대로 붙여 씁니다.

```bash
export GW_REGION="<배포 리전>"
export GW_CLUSTER="llm-gateway-<env>"        # EKS 클러스터 이름
export GW_KCTX="llm-gateway-<env>"           # kube context
export GW_NS="llm-gateway"
export GW_ENV_DIR="deployment/terraform/environments/<env 디렉토리>"
export AWS_DEFAULT_REGION="$GW_REGION" AWS_REGION="$GW_REGION"
kc() { kubectl --context "$GW_KCTX" -n "$GW_NS" "$@"; }
```

## 0. 규칙 — 깨면 apply가 거부되거나 롤백이 막힙니다

1. **minor는 한 칸씩만.** 다중 점프는 API가 거부 (1.34→1.36 = 2회 순차 홉).
2. **홉 순서 고정**: ① 컨트롤플레인 minor → ② `eks_addon_versions`를 그 버전용으로
   갱신 → ③ **Fargate 파드 전량 재생성**(kubelet은 파드 재생성 때만 갱신).
   ②를 ①보다 먼저 하면 kube-proxy가 apiserver보다 새 버전이 돼 skew 위반.
3. **롤백 제한**: in-place 롤백은 업그레이드 **7일 이내 + ERROR insight 0**일
   때만 직전 minor로 가능. 단 Fargate에서 ③까지 했으면 새 kubelet의 파드가
   skew ERROR insight를 띄워 롤백이 막힘 — **롤백 = 계획된 다운타임**(새 파드
   제거 필요). "7일 롤백 가능"을 "안전하고 공짜"로 읽지 말 것.
4. **선언값=라이브 원칙**: tfvars의 `eks_cluster_version`이 라이브보다 낮으면
   다음 terraform apply가 `InvalidParameterException`으로 죽어 그 스택 전체가
   잠깁니다. 라이브가 이미 올라간 상태면 tfvars를 라이브에 맞춰 올릴 것 —
   `./deploy doctor`가 이 skew를 HIGH로 잡습니다.
5. **extended support**: 표준지원 종료 버전은 시간이 지나면 AWS가 **강제
   업그레이드(롤백 불가)**하거나 시간당 프리미엄($0.50/hr/클러스터)을 청구.
   최신 지원 달력은 AWS EKS 문서 "Kubernetes versions" 참조.

## 1. 사전 점검

```bash
aws sts get-caller-identity                     # 대상 계정 확인
aws eks update-kubeconfig --name "$GW_CLUSTER" --region "$GW_REGION" --alias "$GW_KCTX"

# 라이브 버전 실측 — tfvars가 아니라 AWS가 정본
aws eks describe-cluster --name "$GW_CLUSTER" --region "$GW_REGION" \
  --query 'cluster.version' --output text
for a in coredns kube-proxy vpc-cni; do
  aws eks describe-addon --cluster-name "$GW_CLUSTER" --region "$GW_REGION" \
    --addon-name "$a" --query 'addon.{n:addonName,v:addonVersion,s:status}' \
    --output text
done

# 업그레이드 적합성 검사 (deprecated API·호환성 문제)
aws eks list-insights --cluster-name "$GW_CLUSTER" --region "$GW_REGION" \
  --query 'insights[?status==`PASSING`].name' --output table

# 롤백 보험
helm get values llm-gateway -n "$GW_NS" --kube-context "$GW_KCTX" -o yaml \
  > ~/"$GW_KCTX"-values-$(date +%Y%m%d-%H%M).yaml
```

> **target 버전의 애드온 버전 확인** — 아래 표는 실측 기록일 뿐, 신버전은 AWS가
> 추가/만료합니다. 홉 전에 반드시 실측:
> `aws eks describe-addon-versions --kubernetes-version <목표.minor> --addon-name <name>`

| cluster | coredns | kube-proxy | vpc-cni |
|---|---|---|---|
| 1.32 | v1.11.4-eksbuild.51 | v1.32.13-eksbuild.24 | v1.22.4-eksbuild.3 |
| 1.33 | v1.12.4-eksbuild.29 | v1.33.10-eksbuild.21 | (동일) |
| 1.34 | v1.12.4-eksbuild.29 | v1.34.6-eksbuild.21 | (동일) |
| 1.35 | v1.13.2-eksbuild.21 | v1.35.3-eksbuild.21 | (동일) |
| 1.36 | v1.14.3-eksbuild.14 | v1.36.0-eksbuild.17 | (동일) |

(실측 시점 ap-northeast-2 — 리전별 제공 버전이 다를 수 있음)

## 2. 한 홉 실행 (반복 단위 — dev 먼저, prod는 검증 후)

```bash
cd "$GW_ENV_DIR"

# terraform.tfvars 편집 — 선언을 목표 버전으로:
#   eks_cluster_version = "1.<목표>"
#   eks_addon_versions = { <§1 표/실측의 목표 버전용 값> }

# ⚠️ managed overlay 유지 — bootstrap이 gen에 쓴 infra/feature 키가
#    빠지면 기본값으로 되돌아간다. 파일이 있으면 반드시 같이 넘긴다:
GEN_TFVARS="../../../gen/<env>/eks/terraform.tfvars"
VARGEN=""; [ -f "$GEN_TFVARS" ] && VARGEN="-var-file=$GEN_TFVARS"

terraform plan  -var-file=terraform.tfvars $VARGEN
terraform apply -var-file=terraform.tfvars $VARGEN
# → 컨트롤플레인 ① + 애드온 ② 가 한 번에 적용 (모듈이 순서 보장)
```

③ **Fargate 파드 전량 재생성** — kubelet은 파드가 재생성될 때만 새 버전을
얻습니다. 게이트웨이 포함 네임스페이스 전체를 굴립니다:

```bash
kubectl --context "$GW_KCTX" -n "$GW_NS" rollout restart deploy --all
# observability 등 다른 네임스페이스도 동일하게
kc get pods -o jsonpath='{range .items[*]}{.spec.nodeName}{"\n"}{end}' | sort -u
# ↑ Fargate 노드(kubelet) 버전이 목표 minor로 전부 교체됐는지:
kc get nodes 2>/dev/null || kubectl --context "$GW_KCTX" get nodes
```

검증:

```bash
aws eks describe-cluster --name "$GW_CLUSTER" --region "$GW_REGION" \
  --query 'cluster.version' --output text      # 목표 minor
./deploy doctor                              # version skew HIGH 사라져야 함
./deploy apply --plan                        # 앱 드리프트 없는지
# 운영 중이면 smoke: deployment/scripts/smoke-test.sh --env <env>
```

다음 minor로 갈 거면 §2를 **다시** 반복 — 한 홉씩.

## 3. Aurora 엔진 (의존성 업그레이드)

`aurora_engine_version`(현재 기본 `16.11`)도 env tfvars로 올립니다 —
컨트롤플레인 홉과는 **독립 작업**이며 같은 apply에 묶지 않는 게 좋습니다
(장애 격리).

```bash
aws rds create-db-cluster-snapshot --region "$GW_REGION" \
  --db-cluster-identifier <aurora 클러스터 id> \
  --db-cluster-snapshot-identifier "pre-pg<목표>-$(date +%Y%m%d-%H%M)"
# terraform.tfvars: aurora_engine_version = "<목표>" → §2와 같은 apply
# ⚠️ writer 재기동 = 짧은 다운타임 — 유지보수 창에서
```

ElastiCache 엔진 교체도 terraform 변수(`elasticache_*`) 소유 — 별도 apply로.

## 4. prod 적용 전 체크리스트

- [ ] dev에서 같은 홉을 먼저 올리고 최소 수일 관찰했는가
- [ ] prod `terraform.tfvars`의 버전이 dev가 아니라 **prod 라이브** 기준인가
- [ ] helm values 백업 + (DB 건드리면) Aurora 스냅샷 찍었는가
- [ ] `list-insights`에 UPGRADE_READINESS 실패 항목이 없는가
- [ ] 롤백이 필요하면 "7일 이내 + 파드 재생성 전"이어야 한다는 걸 알고 창을 잡았는가

## 참고 — 자동 감지

- `./deploy doctor` — tfvars `eks_cluster_version` ≠ 라이브면 **HIGH**로 보고
  ("tfvars를 라이브로 올리고 eks_addon_versions 갱신" 지시 출력)
- 세부 규칙의 근거(다운그레이드 가드·nullable 함정·버전 표 출처)는
  `deployment/terraform/environments/*/variables.tf`의 변수 주석이 정본 —
  표가 낡았으면 `describe-addon-versions` 실측이 우선합니다.
