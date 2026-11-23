# EKS 클러스터·의존성 업그레이드 런북

> **범위**: EKS 컨트롤플레인 minor · EKS 애드온(coredns/kube-proxy/vpc-cni) ·
> Fargate kubelet · Aurora 엔진 — `./deploy apply`가 하지 않습니다
> (앱 업데이트는 [update.md](update.md)). 클러스터+애드온은
> **`./deploy upgrade eks`** 가 절차를 자동 수행하고(§1), Aurora·ElastiCache
> 엔진은 terraform env 수동 절차입니다(§3).

## 0. 규칙 — `deploy upgrade eks`가 강제하는 것들

1. **minor는 한 칸씩만.** 다중 점프는 API가 거부 (1.34→1.36 = 2회 순차 홉).
   명령이 라이브에서 목표까지 홉을 자동 분할합니다.
2. **홉 순서 고정**: ① 컨트롤플레인 minor → ② `eks_addon_versions`를 그 버전용으로
   갱신 → ③ **Fargate 파드 전량 재생성**(kubelet은 파드 재생성 때만 갱신).
   홉마다 동일 순서를 반복합니다.
3. **롤백 제한**: in-place 롤백은 업그레이드 **7일 이내 + ERROR insight 0**일
   때만 직전 minor로 가능. 단 Fargate에서 ③까지 했으면 새 kubelet의 파드가
   skew ERROR insight를 띄워 롤백이 막힘 — **롤백 = 계획된 다운타임**(새 파드
   제거 필요). "7일 롤백 가능"을 "안전하고 공짜"로 읽지 말 것.
4. **선언값=라이브 원칙**: tfvars의 `eks_cluster_version`이 라이브보다 낮으면
   다음 terraform apply가 `InvalidParameterException`으로 죽어 그 스택 전체가
   잠깁니다. 명령이 tfvars를 홉마다 갱신하고, 완료 시 `gateway.yaml`의
   `infra.eks_version`도 라이브로 맞춥니다.
5. **extended support**: 표준지원 종료 버전은 시간이 지나면 AWS가 **강제
   업그레이드(롤백 불가)**하거나 시간당 프리미엄($0.50/hr/클러스터)을 청구.
   최신 지원 달력은 AWS EKS 문서 "Kubernetes versions" 참조.

## 1. 표준 경로 — `./deploy upgrade eks`

```bash
./deploy upgrade eks --to 1.<목표> --plan   # 홉 계획 + 애드온 버전 실측 확인
./deploy upgrade eks --to 1.<목표>          # 확인 프롬프트 후 실행
./deploy upgrade eks                        # 목표 생략 시 yaml infra.eks_version
```

각 홉에서 명령이 하는 일:

1. env `terraform.tfvars`에 `eks_cluster_version` + `eks_addon_versions` 기록
   — 애드온은 `describe-addon-versions`의 목표 k8s `defaultVersion` 조합,
   단 **현재 핀이 목표와 호환이면 유지**(비호환 고핀은 다운그레이드하지
   않고 중단). tfvars 부재 시 중단(access_entries 등 보호).
2. `terraform plan -out` → **범위 검사**(module.eks 밖 변경·destroy/
   replace·moved/import 발견 시 중단 — `time_sleep` 재생성은 무해라 예외)
   → 저장된 plan만 apply.
3. 클러스터 `ACTIVE`+버전 일치 대기 (홉당 통상 10~20분).
4. 전체 deployment/statefulset `rollout restart` + **워크로드별
   `rollout status` 대기** — Fargate kubelet 갱신. 파드가 전부 재생성되므로
   `capacity_tier`/`sizing`의 minReplicas가 1이면 순간 결손 — 유지보수 창 권장.

시작 전 자동 사전 점검: upgrade insights ERROR(제거 API 사용 등), kubectl
context가 다른 클러스터를 가리키는지, `deploy.tf_env_dir` 선언 불일치.

중간 실패 시 재실행하면 라이브 버전+애드온 실측으로 **남은 단계부터
재개**됩니다(컨트롤플레인만 올라간 반쪽 홉도 복구). tfvars에는
`.tfvars.bak` 백업이 남습니다.

### kubectl context 주의

파드 재생성은 `kubectl`로 수행 — context가 다른 클러스터를 가리키면
차단됩니다. 명시하려면 `--context <ctx>`.

## 2. 수동 경로 (명령이 막힐 때의 폴백)

`deploy upgrade eks`를 쓸 수 없는 환경(도구 없는 머신·비표준 env)에서의
수동 절차입니다. 기본 원칙은 동일합니다.

```bash
export GW_REGION="<배포 리전>"
export GW_CLUSTER="llm-gateway-<env>"        # EKS 클러스터 이름
export GW_KCTX="llm-gateway-<env>"           # kube context
export GW_NS="llm-gateway"
export GW_ENV_DIR="deployment/terraform/environments/<env 디렉토리>"
export AWS_DEFAULT_REGION="$GW_REGION" AWS_REGION="$GW_REGION"
kc() { kubectl --context "$GW_KCTX" -n "$GW_NS" "$@"; }
```

### 사전 점검

```bash
aws eks describe-cluster --name "$GW_CLUSTER" --region "$GW_REGION" \
  --query 'cluster.version' --output text
for a in coredns kube-proxy vpc-cni; do
  aws eks describe-addon --cluster-name "$GW_CLUSTER" --region "$GW_REGION" \
    --addon-name "$a" --query 'addon.{n:addonName,v:addonVersion,s:status}' \
    --output text
done
aws eks list-insights --cluster-name "$GW_CLUSTER" --region "$GW_REGION" \
  --query 'insights[?status!=`PASSING`].{name:name,status:status,category:category}' \
  --output table
helm get values llm-gateway -n "$GW_NS" --kube-context "$GW_KCTX" -o yaml \
  > ~/"$GW_KCTX"-values-$(date +%Y%m%d-%H%M).yaml
```

> **목표 버전의 애드온 버전 실측** (`--kubernetes-version`은 **목표** 버전,
> `compatibilities[0].defaultVersion=true`가 EKS 검증 기본값):
>
> ```bash
> for a in coredns kube-proxy vpc-cni; do
>   v=$(aws eks describe-addon-versions --kubernetes-version <목표.minor> \
>     --region "$GW_REGION" --addon-name "$a" \
>     --query 'addons[].addonVersions[?compatibilities[0].defaultVersion==`true`].addonVersion | [0][0]' \
>     --output text)
>   printf '  %-11s = "%s"\n' "${a//-/_}" "$v"
> done
> ```

### 한 홉 실행

```bash
cd "$GW_ENV_DIR"
# terraform.tfvars 편집 — eks_cluster_version = "1.<목표>" + eks_addon_versions

# ⚠️ managed overlay 유지 — bootstrap이 gen에 쓴 infra/feature 키가
#    빠지면 기본값으로 되돌아간다. 파일이 있으면 반드시 같이 넘긴다:
GEN_TFVARS="../../../gen/<env>/eks/terraform.tfvars"
VARGEN=""; [ -f "$GEN_TFVARS" ] && VARGEN="-var-file=$GEN_TFVARS"

terraform plan  -var-file=terraform.tfvars $VARGEN
terraform apply -var-file=terraform.tfvars $VARGEN

kubectl --context "$GW_KCTX" -n "$GW_NS" rollout restart deploy --all
# observability 등 다른 네임스페이스도 동일하게 (sts 포함)
```

검증:

```bash
aws eks describe-cluster --name "$GW_CLUSTER" --region "$GW_REGION" \
  --query 'cluster.version' --output text      # 목표 minor
./deploy doctor                              # version skew HIGH 사라져야 함
./deploy apply --plan                        # 앱 드리프트 없는지
```

다음 minor로 갈 거면 같은 절차를 **다시** 반복 — 한 홉씩.

## 3. Aurora 엔진 (의존성 업그레이드)

`aurora_engine_version`(현재 기본 `16.11`)도 env tfvars로 올립니다 —
컨트롤플레인 홉과는 **독립 작업**이며 같은 apply에 묶지 않는 게 좋습니다
(장애 격리).

```bash
aws rds create-db-cluster-snapshot --region "$GW_REGION" \
  --db-cluster-identifier <aurora 클러스터 id> \
  --db-cluster-snapshot-identifier "pre-pg<목표>-$(date +%Y%m%d-%H%M)"
# terraform.tfvars: aurora_engine_version = "<목표>" → terraform apply
# ⚠️ writer 재기동 = 짧은 다운타임 — 유지보수 창에서
```

ElastiCache 엔진 교체도 terraform 변수(`elasticache_*`) 소유 — 별도 apply로.

## 4. prod 적용 전 체크리스트

- [ ] dev에서 같은 홉을 먼저 올리고 최소 수일 관찰했는가
- [ ] `--plan` 출력의 홉 계획·애드온 버전이 기대와 같은가
- [ ] helm values 백업 + (DB 건드리면) Aurora 스냅샷 찍었는가
- [ ] `list-insights`에 UPGRADE_READINESS 실패 항목이 없는가(명령이 자동 점검)
- [ ] 롤백이 필요하면 "7일 이내 + 파드 재생성 전"이어야 한다는 걸 알고 창을 잡았는가

## 참고 — 자동 감지

- `./deploy apply` — yaml `infra.eks_version` ≠ 라이브면 안내 출력
- `./deploy doctor` — tfvars `eks_cluster_version` ≠ 라이브면 **HIGH**로 보고
- `./deploy doctor --capture` — 라이브 버전을 `infra.eks_version`으로 역생성
- 세부 규칙의 근거(다운그레이드 가드·nullable 함정)는
  `deployment/terraform/environments/*/variables.tf`의 변수 주석이 정본.
