# EKS prod 업그레이드 런북 — 레거시 스크립트 배포 → 최신 커밋

대상: `install-eks.sh` 경로로 배포된 기존 EKS 환경을 최신 커밋으로 올린다.
(선언적 `./deploy` + `gateway.yaml` 경로로 온보딩했다면 [install-eks.md](install-eks.md)
"2. 업데이트" 를 따른다 — 이 문서는 그 온보딩 **전**의 1회성 수동 업그레이드 경로다.)

> **환경 변수로 파라미터화** — 맨 위 블록만 채우면 아래 명령을 그대로 붙여 쓸 수 있다.

```bash
# ─── 여기만 채운다 ────────────────────────────────────────────────
export GW_REGION=""          # prod 실제 리전 (예: ap-northeast-2) — 확인법 아래
export GW_CLUSTER="llm-gateway-prod"     # EKS 클러스터 이름
export GW_KCTX="llm-gateway-prod"        # kube context 이름
export GW_NS="llm-gateway"               # namespace
export GW_REL="llm-gateway"              # helm release
export GW_TAG="0ad5689c"                 # 배포할 git short SHA (RDS Proxy fix 포함 — 아래 ⚠️)
export GW_DB_CLUSTER=""                  # Aurora cluster 식별자 (스냅샷용)
export GW_REPO_DIR="$PWD"                # 레포 루트 (projects/awsome-ai-gateway 의 상위)
# ─────────────────────────────────────────────────────────────────
export AWS_DEFAULT_REGION="$GW_REGION" AWS_REGION="$GW_REGION"
kc() { kubectl --context "$GW_KCTX" -n "$GW_NS" "$@"; }
```

> **실제 리전 확인** — 리포의 `values-eks-fargate-prod.yaml` / `terraform.tfvars.example`
> 은 템플릿이다(둘 다 `ap-northeast-2` 라고 쓰여 있어도 실측과 다를 수 있음 —
> dev 는 파일상 ap-northeast-2 인데 실제로는 ap-south-1 에서 돈다). 실제값:
> `grep aws_region <prod terraform env>/terraform.tfvars` 또는
> `aws eks describe-cluster --name $GW_CLUSTER --query cluster.arn` 의 ARN 리전 필드.

> ⚠️ **`GW_TAG` 는 `0ad5689c` 이상 필수** — 그 미만의 migration 이미지는
> `PGOPTIONS`(startup packet)를 쓰고, **RDS Proxy 가 이를 거부**해 migration Job 이
> 즉사한다 (`Feature not supported: ... command-line options`, dev migration-223 실측).
> `0ad5689c` 부터 세션 `SET` 방식이라 프록시를 통과한다.

---

## 0. 사전 조건 (prod 배포 EC2, 1회)

```bash
helm version --short                    # v3.14+ 필수 (--reset-then-reuse-values)
aws sts get-caller-identity             # prod 계정인지 확인 — dev 계정이면 중단
aws eks update-kubeconfig --name "$GW_CLUSTER" --region "$GW_REGION" --alias "$GW_KCTX"
kubectl config use-context "$GW_KCTX"
kc get pods                             # 접속 확인

cd "$GW_REPO_DIR"
git fetch && git checkout "$GW_TAG"     # 사용자가 push 한 커밋 기준
cd projects/awsome-ai-gateway
```

## 1. 스냅샷 + R24 선제 정리

```bash
# 1-1. 롤백 보험 — 릴리스 저장 values 는 실측값이 들어있는 유일한 원본
helm get values "$GW_REL" -n "$GW_NS" --kube-context "$GW_KCTX" \
  -o yaml > ~/prod-values-backup-$(date +%Y%m%d-%H%M).yaml
helm history "$GW_REL" -n "$GW_NS" --kube-context "$GW_KCTX" | tail -3

# 1-2. Aurora 스냅샷 (마이그레이션 전진 전)
aws rds create-db-cluster-snapshot --region "$GW_REGION" \
  --db-cluster-identifier "$GW_DB_CLUSTER" \
  --db-cluster-snapshot-identifier "pre-upgrade-$(date +%Y%m%d-%H%M)"

# 1-3. stale ownerRef 제거 (R24) — 구 chart 의 ExternalSecret(creationPolicy: Owner)이
#      만든 Secret 에 ownerRef 가 남아 있으면, upgrade 시 ES 교체에서 GC 가 Secret 을
#      지우는 창이 생긴다. 현재 ownerRef 가 있을 때만 제거.
for sec in llm-gateway-app llm-gateway-db llm-gateway-redis; do
  owner=$(kc get secret "$sec" -o jsonpath='{.metadata.ownerReferences[0].kind}' 2>/dev/null || true)
  [ "$owner" = "ExternalSecret" ] && \
    kc patch secret "$sec" --type=json -p='[{"op":"remove","path":"/metadata/ownerReferences"}]' \
    && echo "stale ownerRef 제거: $sec"
done
```

## 2. (권장) terraform plan — 인프라 드리프트 확인

이번 델타에서 terraform 도 크게 바뀌었다(+5k 줄). apply 없이 helm 만 올려도 동작은
하지만, plan 에서 아래를 확인해 둔다:

```bash
cd "$GW_REPO_DIR/projects/awsome-ai-gateway/deployment/terraform/environments/llm-gateway-prod"
terraform init -upgrade
terraform plan
# 확인할 것:
#  1) eks_cluster_version / eks_addon_versions — 필수값이 됐다. tfvars 에 없으면
#     plan 이 실패한다. 라이브 버전을 명시: kubectl version --short 의 Server 버전
#     + 호환 addon 버전 (AWS 호환성 표).
#  2) 신규 모듈 body_logging / irsa 의 SES·본문로깅 statement — 전부 변수 opt-in
#     (기본 off)이라 tfvars 를 안 건드리면 생성 0건이어야 한다.
#  3) NAT/서브넷 등 파괴적 변경이 보이면 중단 — 기본값은 기존 동작 유지다.
```

## 3. 이미지 빌드 → prod ECR

```bash
cd "$GW_REPO_DIR/projects/awsome-ai-gateway"
for svc in migration gateway-proxy admin-api admin-ui notification-worker cost-recorder-worker; do
  bash deployment/scripts/rebuild-image.sh "$svc" prod    # DEPLOY 없음 — 아래서 단일 upgrade
done
# 태그 = checkout 된 git HEAD short SHA → $GW_TAG 와 같은지 확인:
git rev-parse --short HEAD
```

## 4. helm upgrade — `-f` 플래그 금지

```bash
helm upgrade "$GW_REL" deployment/charts/llm-gateway \
  -n "$GW_NS" --kube-context "$GW_KCTX" \
  --reset-then-reuse-values \
  --set gatewayProxy.image.tag="$GW_TAG" \
  --set adminApi.image.tag="$GW_TAG" \
  --set scheduler.image.tag="$GW_TAG" \
  --set adminUi.image.tag="$GW_TAG" \
  --set notificationWorker.image.tag="$GW_TAG" \
  --set costRecorderWorker.image.tag="$GW_TAG" \
  --set migration.image.tag="$GW_TAG" \
  --set migration.enabled=true \
  --timeout 15m --wait --atomic
```

> ⚠️ **`-f values-eks-fargate-prod.yaml` 을 넘기지 말 것** — 리포 파일은
> `CHANGE_ME` 플레이스홀더라 저장된 실측값을 덮어쓴다. `--reset-then-reuse-values`
> 가 릴리스 저장 values(설치 시 주입된 실측값)를 유지하면서 신규 차트 기본값만
> 병합한다.

## 5. migration 검증

```bash
# 별도 터미널에서 관찰
watch kc get pods -l app.kubernetes.io/component=migration
kc logs -l app.kubernetes.io/component=migration --tail=50 -f
```

- init SQL 글로빙 → 롤/GRANT → `alembic upgrade head` 순서로 진행.
  dev 기준 22개 신규 리비전이 적용된다(0027–0048).
- 핫 테이블 DDL(0034 인덱스/unique, 0044 bigint, 0045 unique team/dept)은 세션
  `lock_timeout=60s`(기본값)이 보호한다 — 락 큐에 서지 않고 빠르게 실패 →
  Job 재시도로 재개(전 리비전 멱등).
- **0045 사전 조건**: prod `team`/`dept` 이름 중복이 없어야 한다. Job 실패가
  unique 위반이면 중복 정리 후 Job 재실행(파드만 새로 띄우면 됨 — helm 재실행 불요).
- 완료되면 `--wait` 가 helm upgrade 를 통과시킨다 — Job 성공이 곧 검증이다.

## 6. 배포 후 검증

```bash
bash deployment/scripts/smoke-test.sh --env prod --with-bedrock
# prod 는 APP_ENV=prod → /internal/test/issue-key 앱 경로 자동 스킵(의도된 가드)
#   - Pod Ready / health / /v1/models 401 / Ingress / Bedrock E2E 확인
```

- admin-ui 로그인(Cognito OIDC) + 대시보드에서 usage/cost 적재 확인.
- 클라이언트 경로는 gateway-cli 로 실 호출 확인(claude-code 기준; codex/cowork 는
  계정의 Bedrock 모델 접근 범위에 의존).

## 7. 롤백

- `--atomic` 이 migration/pod 실패 시 자동 롤백한다.
- 수동: `helm rollback "$GW_REL" <이전-rev> -n "$GW_NS" --kube-context "$GW_KCTX"`
- **마이그레이션은 롤백되지 않는다(전진만)** — 전 리비전이 additive/멱등이라
  구 앱이 새 스키마 위에서 호환되도록 설계돼 있다. schema 문제 자체가 원인이면
  1-2 의 Aurora 스냅샷으로 복구가 유일한 길이다.

---

## 함정 요약 (dev 실배포 검증에서 확인된 것)

| 함정 | 증상 | 대처 |
|---|---|---|
| RDS Proxy startup options 거부 | migration Job `Feature not supported: ... command-line options` 즉사 | `GW_TAG >= 0ad5689c` |
| values 플레이스홀더 덮어쓰기 | `-f` 로 CHANGE_ME 파일 주면 도메인/ARN 소실 | `--reset-then-reuse-values` 만 사용 |
| 핫 테이블 DDL 락 큐 대기 | migration 이 `usage_logs` 락을 못 잡고 대기 → 뒤의 앱 INSERT 전부 대기 | 세션 `lock_timeout=60s` 자동 적용(기본값), Job 재시도로 재개 |
| 0045 unique team/dept | 기존 중복 데이터가 있으면 migration 실패 | 사전 중복 확인/정리 |
| 구 ExternalSecret ownerRef | upgrade 시 Secret GC 창 | 1-3 선제거 |
| `DB_MIGRATION_*` env | chart 에 미배선 — 기본값(60s/15min/10min)만 동작 | 필요하면 Job 템플릿에 env 추가 |
