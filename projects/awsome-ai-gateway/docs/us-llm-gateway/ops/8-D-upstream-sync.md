# 8-D. upstream 동기화 배포 — 코드·스키마·단가를 한 번에

> ← [operations.md](../operations.md) §8 목차로 · 이 절 = **§8-D** · 소요 ~1.5h(빌드 15분 · helm+migration 10분 · 대기 포함) · **트래픽 적은 시간**에
>
> **한 줄**: upstream 을 크게 들여온 뒤(2026-09: DB 마이그레이션 11개 · 이미지 6종 · 단가) 배포 EC2 에서
> **위에서 아래로 명령만 치는** 순서. 이유·함정·상세는 링크. 서비스 하나 고친 일상 업데이트는 [8-U](8-U-update.md).

```
①저장소 최신화 → ②사전 점검 → ③DB 스냅샷 → ④terraform plan(확인만)
→ ⑤태그 올림(13) → ⑥이미지 6개 빌드 → ⑦install-eks.sh(migration+롤아웃)
→ ⑧단가(08)·시드 alias 정리 → ⑨사후 점검(14)·24h 관찰
```

전제: 배포 EC2 의 `~/awsome-ai-gateway`, `update-scripts/config.env` 가 이 환경(`DEPLOY_ENV=dev`). prod 는 §⑩.

📋 용어: **마이그레이션** = DB 스키마를 한 단계씩 바꾸는 번호 붙은 스크립트(`db/versions/0034_….py` 식). 이번 배포는 0026~0036 의 11개가 `install-eks.sh` 안에서 순서대로 돈다. 아래의 0032·0034 같은 번호는 그 파일 번호다.

## ① 저장소 최신화 — [README §3 ①](../README.md#3-적용하기-배포-ec2-에서) 그대로

▶ 실행
```bash
cd ~/awsome-ai-gateway && git remote -v
V=deployment/charts/llm-gateway/values-eks-fargate-dev.yaml
cp $V ~/values.bak && git fetch origin
git reset --hard origin/us/deploy-fixes && cp ~/values.bak $V
cmp -s $V ~/values.bak && echo "values restored OK" || echo "RESTORE FAILED"
ls docs/us-llm-gateway/update-scripts/1[34]-*.sh
```
기대: `values restored OK` · 마지막 줄에 `13-bump-image-tags.sh` `14-postdeploy-check.sh`. `RESTORE FAILED` 면 멈춘다(values 는 이 EC2 유일본).

📋 참고: EC2 values 에 새로 넣을 키는 없다 — 이번 upstream 이 추가한 값(스트리밍 타임아웃·감사 로그 env)은 chart 기본값으로 충분하고, 태그만 ⑤ 에서 바뀐다.

## ② 사전 점검 — 읽기 전용, 15분

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 00-preflight-check.sh
bash 14-postdeploy-check.sh --save pre
bash 06-persist-annotations.sh
```
기대:
- `00` 의 **「4. Migration pre-check」가 전부 OK**. `XX` 가 하나라도 있으면 진행 금지 — alias 대소문자 중복은 마이그레이션 0034(alias 를 대소문자 구분 없이 유일하게 만드는 인덱스)를, backend 값은 0032(라우팅 backend 허용 목록 갱신)를 실패시킨다.
- `14` 는 지금 `XX` 3~4개(DB 가 아직 옛 마이그레이션 0025 에 있음 · 단가 · system_settings 표 없음)가 **정상**. 목적은 배포 전 숫자를 `snapshots/pre.numbers` 에 남기는 것.
- `06` 은 `already matches`. 아니면 `--apply`([8-U 0단계](8-U-update.md)).

## ③ DB 스냅샷 — 되돌리기의 기준점

helm rollback 은 코드만 되돌린다. 마이그레이션 11개(0026~0036)는 되돌리지 않으므로 DB 는 이 스냅샷으로만 되돌린다(§롤백).

▶ 실행
```bash
SNAP=llm-gateway-dev-pre-sync-$(date +%Y%m%d)
aws rds create-db-cluster-snapshot --db-cluster-identifier llm-gateway-dev \
  --db-cluster-snapshot-identifier $SNAP --query DBClusterSnapshot.Status
aws rds wait db-cluster-snapshot-available --db-cluster-snapshot-identifier $SNAP
```
기대: `"creating"` → wait 가 조용히 끝남(수 분).

## ④ terraform — plan 까지만

이번 배포는 `terraform apply` 가 **필요 없다**(`install-eks.sh` 가 새 output 을 읽지 않는다). plan 은 드리프트를 알기 위해서만.

▶ 실행
```bash
cd ~/awsome-ai-gateway/deployment/terraform/environments/llm-gateway-dev
terraform init
terraform plan | tail -25
```
기대: `0 to destroy`. 나올 수 있는 변경 = `external-secrets` helm_release 갱신(upstream 이 웹훅·cert-controller 비활성 설정을 걷어냄) · IAM 정책 in-place.
**멈추는 조건**: destroy/replace 가 1개라도 · EKS 버전·애드온 변경(tfvars pin → [8-E](8-E-eks-upgrade.md)). `init` 이 lock 을 고쳐 써도 커밋하지 않는다([8-U](8-U-update.md#terraform-output-실패로-멈추면--terraform-apply-를-돌리지-말-것)). apply 는 배포와 별개로 결정한다.

## ⑤ 이미지 태그 올림 — 새 코드는 새 태그로

같은 태그로 rebuild 하면 옛 이미지가 덮여 helm rollback 이 무의미해진다. 표의 `template` 열이 기대값이다(숫자를 외우지 않는다).

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 13-bump-image-tags.sh dev
bash 13-bump-image-tags.sh dev --apply
```
기대: 표 6행 모두 `<- change`(또는 `<- pin explicitly`) → `--apply` 후 `OK values updated` 와 바뀐 줄 목록. 백업 = `snapshots/<ts>-values-dev.yaml.bak`.

## ⑥ 이미지 6개 빌드·push — 15분

▶ 실행
```bash
cd ~/awsome-ai-gateway
for s in migration gateway-proxy admin-api admin-ui notification-worker \
  cost-recorder-worker; do ./deployment/scripts/rebuild-image.sh $s dev || break; done
```
기대: `✅ push 완료: …:<새 태그>` 6번. 스크립트가 끝에 `rollout restart` 를 안내하지만 **하지 않는다** — ⑦ 이 한 번에 올린다. ECR 확인은 [8-P §2-5](8-P-prod.md#2-5-컨테이너-이미지-빌드--ecr-install-guide-3-5) 의 목록 명령.

## ⑦ 배포 — migration + 롤아웃, 10분

▶ 실행
```bash
cd ~/awsome-ai-gateway && ./deployment/scripts/install-eks.sh dev
```
기대: migration Job Completed → Deployment 6개 롤아웃 → `deployed`.
- 순서는 **migration 먼저, 롤아웃 나중**(pre-upgrade hook). 그 사이 수 분간 옛 파드가 모델 목록 조회에 실패할 수 있다(마이그레이션 0032 가 옛 코드가 모르는 provider 값을 심음) — **그 창에서 rollback 하지 않는다**. 새 파드 Ready 로 끝난다.
- Job 실패: `kubectl -n llm-gateway logs job/llm-gateway-migration-<rev>`(`helm history` 최신 rev). 마이그레이션 0034 에서 죽었으면 ② 의 alias 대소문자 중복.

## ⑧ 단가·시드 alias — 5분 + 캐시 5분

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 08-set-model-pricing.sh
bash 08-set-model-pricing.sh --apply
```
기대: 차이 표 → `--apply` 후 검증 행 3개 일치. 왜 배포 **뒤**인가: 마이그레이션(0027 · 0030 — 모델·단가 시드)이 단가 행을 건드릴 수 있어서([US-11](../updates.md)).

admin UI › Models 에 새로 ACTIVE 로 보이는 시드 alias(`global.anthropic.claude-opus-5`·`…-sonnet-5`·`gpt-5.6-*`·`llama-3-70b`)는 **INACTIVE** 로 — US 가 서비스하지 않는다. ⑨ 의 14 가 남은 것을 알려준다.

## ⑨ 사후 점검 — 14 + 종단 1건, 그리고 24h

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 14-postdeploy-check.sh --compare snapshots/pre.numbers
bash 04-verify.sh --base-url https://<gateway-domain> --vk <VK>
```
기대: 14 `OK no failures` · 04 종단 200 + 비용 행. `<VK>` 는 [「VK 얻기」](../update-scripts/README.md#vk-얻기).

| 확인 | 어떻게 | 기대 |
|---|---|---|
| 스키마 | 14 `alembic_version` | `0036`(마지막 마이그레이션 번호 = repo 의 최신) |
| 단가 | 14 price 행 · 호출 1건 뒤 `usage_logs.cost_usd` 검산 | pricing.tsv 와 일치 |
| 라우팅 | 14 `claude-… -> us.anthropic.…` | `us.` 그대로(Global 로 안 바뀜) |
| web search | Claude Code 로 검색 필요한 질문 1건 → 14 의 W(24h) | `web_search_count` ≥ 1 |
| thinking | `thinking:{type:enabled}` 요청 1건 | 200(400 아님) |
| readiness | 14 §4 `/health/ready` | 200 `HEALTHY` |
| 예산 | 14 U/B 비율 | 경고 없음(≤ 1.05) |
| 24h | 다음날 `14 --compare` 다시 | 오류 비율·비용 급변 없음 |

## 롤백

- **코드+DB 전부** — ③ 스냅샷 복원(새 클러스터로 생기므로 RDS Proxy 대상 교체가 따른다, 1h+) **+** `helm rollback`([8-U 4단계](8-U-update.md#4단계--안-되면-되돌린다)). 코드만 되돌리면 옛 이미지가 마이그레이션 0032 가 심은 행을 못 읽어 모델 목록이 깨진다.
- **단가만** — `snapshots/<ts>-08-pricing-rollback.sql` 을 psql 파드로.
- **values 태그만** — `snapshots/<ts>-values-dev.yaml.bak` 복원 후 `install-eks.sh dev`.

## ⑩ prod — prod 계정의 배포 EC2 에서, dev ⑨ 를 하루 지켜본 뒤

dev 와 같은 순서다. 다른 것은 셋뿐 — values 파일 이름, 스냅샷·terraform 디렉터리의 `prod`, 스크립트 인자 `prod`. `config.env` 는 prod EC2 의 것(`DEPLOY_ENV="prod"`)을 쓴다.

**⑩-① 저장소 최신화**
▶ 실행
```bash
cd ~/awsome-ai-gateway && git remote -v
V=deployment/charts/llm-gateway/values-eks-fargate-prod.yaml
cp $V ~/values.bak && git fetch origin
git reset --hard origin/us/deploy-fixes && cp ~/values.bak $V
cmp -s $V ~/values.bak && echo "values restored OK" || echo "RESTORE FAILED"
```

**⑩-② 사전 점검**
▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
grep -n '^DEPLOY_ENV' config.env
bash 00-preflight-check.sh
bash 14-postdeploy-check.sh --save pre
bash 06-persist-annotations.sh
```
기대: `DEPLOY_ENV="prod"` · 「4. Migration pre-check」 전부 OK · `06` 은 `already matches`.

**⑩-③ DB 스냅샷**
▶ 실행
```bash
SNAP=llm-gateway-prod-pre-sync-$(date +%Y%m%d)
aws rds create-db-cluster-snapshot --db-cluster-identifier llm-gateway-prod \
  --db-cluster-snapshot-identifier $SNAP --query DBClusterSnapshot.Status
aws rds wait db-cluster-snapshot-available --db-cluster-snapshot-identifier $SNAP
```

**⑩-④ terraform plan 까지만**
▶ 실행
```bash
cd ~/awsome-ai-gateway/deployment/terraform/environments/llm-gateway-prod
terraform init
terraform plan | tail -25
```
기대·멈추는 조건은 ④ 와 같다.

**⑩-⑤ 태그 올림**
▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 13-bump-image-tags.sh prod
bash 13-bump-image-tags.sh prod --apply
```

**⑩-⑥ 이미지 6개 빌드·push**
▶ 실행
```bash
cd ~/awsome-ai-gateway
for s in migration gateway-proxy admin-api admin-ui notification-worker \
  cost-recorder-worker; do ./deployment/scripts/rebuild-image.sh $s prod || break; done
```

**⑩-⑦ 배포**
▶ 실행
```bash
cd ~/awsome-ai-gateway && ./deployment/scripts/install-eks.sh prod
```

**⑩-⑧ 단가 · 시드 alias**
▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 08-set-model-pricing.sh
bash 08-set-model-pricing.sh --apply
```
그다음 admin UI › Models 에서 시드 alias INACTIVE(⑧ 과 같음).

**⑩-⑨ 사후 점검**
▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 14-postdeploy-check.sh --compare snapshots/pre.numbers
bash 04-verify.sh --base-url https://gateway-prod.<도메인> --vk <VK>
```
기대와 기능별 확인 표는 ⑨ 와 같다. 롤백은 §롤백에서 `dev`→`prod`(스냅샷 `llm-gateway-prod-pre-sync-…`, 백업 `snapshots/<ts>-values-prod.yaml.bak`).
