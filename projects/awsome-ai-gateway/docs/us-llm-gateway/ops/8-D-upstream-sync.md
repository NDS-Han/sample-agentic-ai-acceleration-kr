# 8-D. upstream 동기화 배포 — 코드·스키마·단가를 한 번에

> ← [operations.md](../operations.md) §8 목차로 · 이 절 = **§8-D** · 소요 ~1.5h(빌드 15분 · helm+migration 10분 · 대기 포함) · **트래픽 적은 시간**에
>
> **한 줄**: upstream 을 크게 들여온 뒤(2026-09: DB 마이그레이션 11개 · 이미지 6종 · 단가) 배포 EC2 에서
> **위에서 아래로 명령만 치는** 순서. 이유·함정·상세는 링크. 서비스 하나 고친 일상 업데이트는 [8-U](8-U-update.md).

```
(1) 저장소 최신화 → (2) 사전 점검·설정 적용 → (3) DB 스냅샷
→ (4) terraform plan(확인만) → (5) 태그 올림(13) → (6) 이미지 6개 빌드
→ (7) install-eks.sh(migration+롤아웃) → (8) 단가(08)·시드 alias 정리
→ (9) 사후 점검(14)·24h 관찰
```

전제: 배포 EC2 의 `~/awsome-ai-gateway`, `update-scripts/config.env` 가 이 환경(`DEPLOY_ENV=dev`). prod 는 §(10).

📋 용어: **마이그레이션** = DB 스키마를 한 단계씩 바꾸는 번호 붙은 스크립트(`db/versions/0034_….py` 식). 이번 배포는 0026~0036 의 11개가 `install-eks.sh` 안에서 순서대로 돈다. 아래의 0032·0034 같은 번호는 그 파일 번호다.

## US-18 로 따라 할 때 달라지는 것

US-18 도 아래 (1)부터 (9)까지를 순서대로 따라 합니다. 이번 업데이트가 바꾸는 것은
**이미지 5개 · DB 열 추가 3개(0037·0038·0039) · Sonnet 5.5 캐시 읽기 단가 1개**입니다.
본문과 다른 점은 각 단계 맨 앞의 **US-18** 줄에 있습니다. prod 의 (10-1)부터 (10-9)까지도 같습니다.

## (1) 저장소 최신화 — [README §3 의 「저장소 최신화」](../README.md#3-적용하기-배포-ec2-에서)와 같음

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

📋 참고: 이번 upstream 이 추가한 값(스트리밍 타임아웃·감사 로그 env)은 chart 기본값으로 충분하다. 단 **DB 마스터 비밀번호 참조 2줄**은 values 에 있어야 한다 — (2) 의 `15` 가 확인·삽입한다. 태그는 (5) 에서.

## (2) 사전 점검 · 설정 적용 — values 파일까지만, 15분

> **US-18** — `14-postdeploy-check.sh --save pre` 의 `XX` 가 2개(스키마 `0036` · Sonnet 5.5 단가)면
> 정상입니다. 둘 다 이번 업데이트가 고칩니다.

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 00-preflight-check.sh
bash 14-postdeploy-check.sh --save pre
bash 06-persist-annotations.sh
bash 15-set-master-secret-ref.sh
bash 17-set-websearch-caps.sh
bash 17-set-websearch-caps.sh --apply
```
기대:
- `00`·`14` 는 읽기 전용, `06`·`15`·`17` 은 **values 파일만** 고친다(클러스터 반영은 7단계).
- `00` 의 **「4. Migration pre-check」가 전부 OK**. `XX` 가 하나라도 있으면 진행 금지 — alias 대소문자 중복은 마이그레이션 0034(alias 를 대소문자 구분 없이 유일하게 만드는 인덱스)를, backend 값은 0032(라우팅 backend 허용 목록 갱신)를 실패시킨다.
- `14` 는 지금 `XX` 3~4개(DB 가 아직 옛 마이그레이션 0025 에 있음 · 단가 · system_settings 표 없음)가 **정상**. 목적은 배포 전 숫자를 `snapshots/pre.numbers` 에 남기는 것.
- `06` 은 `already matches`. 아니면 `--apply`([8-U 0단계](8-U-update.md)).
- `15` 는 표 3행(masterPasswordRemoteKey · masterPasswordRemoteProperty · masterUser)에 `<- change` 없이 `OK … nothing to do`. 새 차트의 migration Job 은 init SQL·권한 부여를 **DB 마스터 사용자**로 실행하므로, values 가 그 비밀번호를 RDS 가 직접 로테이션하는 시크릿(`rds!cluster-<uuid>`)에서 가져오게 돼 있어야 한다(`database.external.masterPasswordRemoteKey`·`…RemoteProperty`). `<- change` 가 있으면 `bash 15-set-master-secret-ref.sh --apply`(백업 후 삽입, helm 렌더로 검증) — 없이 (7) 을 돌리면 migration Job 이 5분 타임아웃으로 죽는다.
- `17` 은 기존 배포에선 표의 행(web search 상한 4개 — 결과 크기·결과 수·턴당 검색 수·반복 수 — 와 동작 스위치)이 `<- change` 로 나온다 — 그래서 `--apply` 까지 두 줄(`yes` 입력, 백업 후 values 에 삽입, helm 렌더로 검증). 이미 들어 있으면 `nothing to do` 로 지나간다. (7) 의 롤아웃에서 적용되고, **(7) 을 이미 끝낸 뒤라면 `install-eks.sh` 를 한 번 더** 돌린다. **9/19 부터(gateway-proxy 1.0.80 이상) 이 목표값이 코드 기본값과 같다** — `rendered now` 열에 `(unset -> code default …)` 로 보이는 행은 넣지 않아도 동작이 같고, `--apply` 는 같은 값을 values 에 명시해 둘 뿐이다(값을 바꾸거나 스위치를 끌 때가 `17` 의 용도). 검색 결과가 다음 턴 입력으로 되돌아와 반복마다 과금되는 구조라, **1.0.80 이전 이미지의** 기본값(60,000자·10개·4회·5회)으로는 검색 질문 1건이 $3 를 넘긴다(2026-09-16 실측 $3.62) — 12,000자·5개·3회·2회(요청당 최대 6회)면 약 $1.5 이하. 값은 `config.env` 의 `WEB_SEARCH_*`.
- 단가의 정본은 **파일 하나** — `docs/us-llm-gateway/update-scripts/pricing.tsv`(alias 별 입력·출력·캐시 단가, /1K, US `us.` Standard 티어). `14` 와 `08` 은 이 파일과 DB 를 비교한다. 다른 리전·티어로 청구받는 배포라면 **(8) 전에** 이 파일을 자기 청구 단가로 고친다(`asof`·`source` 열 포함) — 그러면 `08` 은 "차이 없음", `14` 는 OK.

## (3) DB 스냅샷 — 되돌리기의 기준점

> **US-18** — `SNAP=` 줄의 `pre-sync` 를 `pre-us18` 로 바꿔 칩니다(롤백 때 이 이름으로 찾습니다).

helm rollback 은 코드만 되돌린다. 마이그레이션 11개(0026~0036)는 되돌리지 않으므로 DB 는 이 스냅샷으로만 되돌린다(§롤백).

▶ 실행
```bash
SNAP=llm-gateway-dev-pre-sync-$(date +%Y%m%d)
aws rds create-db-cluster-snapshot --db-cluster-identifier llm-gateway-dev \
  --db-cluster-snapshot-identifier $SNAP --query DBClusterSnapshot.Status
aws rds wait db-cluster-snapshot-available --db-cluster-snapshot-identifier $SNAP
aws rds describe-db-cluster-snapshots --db-cluster-snapshot-identifier $SNAP \
  --query 'DBClusterSnapshots[0].[DBClusterSnapshotIdentifier,Status]' --output text
```
기대: `"creating"` → wait 가 조용히 끝남(수 분) → 마지막 줄 `llm-gateway-dev-pre-sync-<날짜>  available`. 이 이름을 §롤백에서 쓴다.

## (4) terraform — 인프라 변경 확인 (필요할 때만 apply)

> **US-18** — `exit=0` 과 `No changes.` 가 나와야 합니다. 그러면 apply 없이 (5) 로 넘어갑니다.

▶ 실행
```bash
cd ~/awsome-ai-gateway/deployment/terraform/environments/llm-gateway-dev
terraform init
terraform plan -no-color -detailed-exitcode > ~/plan.txt 2>&1; echo "exit=$?"
grep -E '# .* (will be|must be)|^Plan:|No changes|Error' ~/plan.txt
```
`exit=` 값으로 판정합니다.
- `0` — 변경 없음. apply 하지 않고 (5) 로 넘어갑니다.
- `1` — plan 오류. `Error` 줄을 보고 멈춥니다(출력이 비면 terraform 폴더 밖에서 친 것입니다).
- `2` — 변경 있음. 아래 「멈출 때」에 해당하지 않으면 `terraform apply`(`yes` 입력)로 적용하고,
  위 plan 두 줄을 다시 쳐서 `exit=0` 을 확인합니다.

멈출 때(apply 하지 않습니다):
- 이유를 모르는 destroy·replace
- EKS 버전·애드온 변경 — tfvars 의 버전 고정을 확인합니다([8-E](8-E-eks-upgrade.md)).
- `external-secrets` 줄 — fork 의 ESO OFF 설정이 덮인 것입니다. `modules/external-secrets/main.tf`
  의 3줄을 복원한 뒤 다시 plan 합니다(켜면 cert-controller 가 0/1 로 멈춥니다).

## (5) 이미지 태그 올림 — 새 코드는 새 태그로

> **US-18** — `<- change` 는 5행(migration · gateway-proxy · admin-api · admin-ui ·
> cost-recorder-worker)입니다. notification-worker 는 바뀌지 않습니다.

같은 태그로 rebuild 하면 옛 이미지가 덮여 helm rollback 이 무의미해진다. 표의 `template` 열이 기대값이다(숫자를 외우지 않는다).

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 13-bump-image-tags.sh dev
bash 13-bump-image-tags.sh dev --apply
```
기대: 표 6행 모두 `<- change`(또는 `<- pin explicitly`) → `--apply` 후 `OK values updated` 와 바뀐 줄 목록. 백업 = `snapshots/<ts>-values-dev.yaml.bak`.

📋 참고(동기화 담당자용): upstream 이 코드를 바꾸고도 태그를 안 올린 서비스는 13 이 "변경 없음" 으로 본다. 리베이스 뒤 서비스마다 `git log <이전 배포 base>..HEAD -- <서비스 디렉터리>` 로 코드 변경을 보고, 변경됐는데 템플릿 태그가 그대로면 **fork 템플릿(dev·prod)에서 새 태그를 매긴 뒤** (5) 를 돈다. 같은 태그로 rebuild 하면 helm 이 변화를 못 봐 롤아웃이 없고(옛 코드 계속 실행) 옛 이미지만 덮인다(2026-09-15: notification-worker·cost-recorder-worker 가 그 경우). upstream 이 태그 이후 한 번도 빌드하지 않은 커밋은 우리가 첫 실행이 되므로 (9) 의 기능 확인을 생략하지 않는다(같은 날 admin-ui 미들웨어 500 이 그렇게 잡혔다).

## (6) 이미지 6개 빌드·push — 15분

> **US-18** — 아래 본문 명령 대신 이 명령을 칩니다(notification-worker 를 빼고 5개만 —
> 태그가 그대로인 이미지를 다시 빌드하면 옛 이미지를 덮어써 되돌릴 수 없습니다).
> ```bash
> cd ~/awsome-ai-gateway
> for s in migration gateway-proxy admin-api admin-ui cost-recorder-worker; do
>   ./deployment/scripts/rebuild-image.sh $s dev || break; done
> ```

▶ 실행
```bash
cd ~/awsome-ai-gateway
for s in migration gateway-proxy admin-api admin-ui notification-worker \
  cost-recorder-worker; do ./deployment/scripts/rebuild-image.sh $s dev || break; done
```
기대: `✅ push 완료: …:<새 태그>` 6번. 스크립트가 끝에 `rollout restart` 를 안내하지만 **하지 않는다** — (7) 이 한 번에 올린다. ECR 확인은 [8-P §2-5](8-P-prod.md#2-5-컨테이너-이미지-빌드--ecr-install-guide-3-5) 의 목록 명령.

## (7) 배포 — migration + 롤아웃, 10분

▶ 실행
```bash
cd ~/awsome-ai-gateway/deployment/terraform/environments/llm-gateway-dev
terraform init | tail -3
terraform output -json >/dev/null && echo "output OK"
cd ~/awsome-ai-gateway && ./deployment/scripts/install-eks.sh dev
```
기대: `output OK` → migration Job Completed → Deployment 6개 롤아웃 → `deployed`.
- `terraform init` 을 다시 하는 이유: (1) 의 `git reset --hard` 가 `.terraform.lock.hcl` 을 커밋본으로 되돌려, 그대로 두면 `install-eks.sh` 가 `terraform output 실패` 로 멈춘다(인프라·state 무관 — [8-U](8-U-update.md#terraform-output-실패로-멈추면--terraform-apply-를-돌리지-말-것)). init 은 lock 만 고쳐 쓴다.
- 순서는 **migration 먼저, 롤아웃 나중**(pre-upgrade hook). 그 사이 수 분간 옛 파드가 모델 목록 조회에 실패할 수 있다(마이그레이션 0032 가 옛 코드가 모르는 provider 값을 심음) — **그 창에서 rollback 하지 않는다**. 새 파드 Ready 로 끝난다.
- Job 실패: `kubectl -n llm-gateway logs job/llm-gateway-migration-<rev>`(`helm history` 최신 rev). 마이그레이션 0034 에서 죽었으면 (2) 의 alias 대소문자 중복.

## (8) 단가·시드 alias — 5분 + 캐시 5분

> **US-18** — 차이 표에 `claude-sonnet-5-5` 캐시 읽기 `0.000220 → 0.000110` 한 줄이면 정상입니다.

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 08-set-model-pricing.sh
bash 08-set-model-pricing.sh --apply
```
기대: 차이 표 → `--apply` 후 검증 행 3개 일치. 왜 배포 **뒤**인가: 마이그레이션(0027 · 0030 — 모델·단가 시드)이 단가 행을 건드릴 수 있어서([US-11](../updates.md)). 단가 표 = `update-scripts/pricing.tsv` 가 정본이다 — 청구 단가가 다르면 `--apply` 전에 이 파일부터 고친다(2단계). 단가만 따로 다룬 문서 = [8-R](8-R-pricing.md).

admin UI › Models 에 새로 ACTIVE 로 보이는 시드 alias(`global.anthropic.claude-opus-5`·`…-sonnet-5`·`gpt-5.6-*`·`llama-3-70b`)는 **INACTIVE** 로 — US 가 서비스하지 않는다. (9) 의 14 가 남은 것을 알려준다.

## (9) 사후 점검 — 14 + 종단 1건, 그리고 24h

> **US-18** — 아래 표에 더해 두 가지를 더 봅니다.
> 예산 알림 기준(임계값을 50 으로 저장 → 6분 뒤 다시 열어도 50) · 분석 화면(30초 안에 두 번
> 열어도 정상, 앱 필터를 바꾸면 숫자가 바뀜).

▶ 실행 · EC2 — 자동 점검과 접속값
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 14-postdeploy-check.sh --compare snapshots/pre.numbers
bash 07-client-values.sh
```
기대: 14 는 `OK no failures`. 07 출력 끝의 `export OIDC_ISSUER_URL=…` `export OIDC_CLIENT_ID=…`
`export ADMIN_API_URL=…` `export ANTHROPIC_BASE_URL=…` 4줄을 복사합니다.

▶ 실행 · Mac — VK 받기 (`gateway-cli` 가 설치된 새 터미널 탭에 위 4줄을 먼저 붙여 넣은 뒤)
```bash
gateway-cli login --redirect-port 8090
api-key-helper 2>/dev/null | grep -m1 '^vk-'
```
브라우저에 Cognito 로그인 창 → `Login successful` → `vk-…` 한 줄이 VK 입니다.
- 8090 이 점유돼 있으면 `--redirect-port 8091`(US 풀 등록 포트 8090·8091).
- `Missing required OIDC config` 로 실패하면 4줄을 같은 터미널에 넣지 않은 것입니다.
- 다 쓰면 이 탭은 닫습니다. `ANTHROPIC_BASE_URL` 이 남아 있으면 그 탭에서 쓰는 Claude Code 가 401 이 납니다.
- Cowork 가 `Credential helper exited with code 1` 이면 같은 로그인을 한 뒤 Cowork 를 Cmd+Q 로 껐다 켭니다(`setup` 재실행 금지).

▶ 실행 · EC2 — 종단 1건 (`<VK>` 자리에 위에서 받은 값을, 꺾쇠까지 지우고 넣습니다)
```bash
GW=https://$(kubectl -n llm-gateway get ingress llm-gateway-gateway -o jsonpath='{.spec.rules[0].host}')
bash 04-verify.sh --base-url $GW --vk <VK>
```
기대: `HTTP 200` + 「C」 표에 새 행(비용 = 입력·출력 토큰 × Standard 단가 — 예: 491/10 토큰이면
0.002976). 꺾쇠를 그대로 두면 bash 리다이렉션 오류가 납니다.

| 확인 | 어떻게 | 기대 |
|---|---|---|
| 스키마 | 14 `alembic_version` | repo 의 최신 마이그레이션 번호(US-18 은 `0039`) |
| 단가 | 14 price 행 · 호출 1건 뒤 `usage_logs.cost_usd` 검산 | pricing.tsv 와 일치 |
| 라우팅 | 14 `claude-… -> us.anthropic.…` | `us.` 그대로(Global 로 안 바뀜) |
| web search | Claude Code 로 검색 필요한 질문 1건 → 14 의 W(24h) | `web_search_count` ≥ 1 |
| thinking | `thinking:{type:enabled}` 요청 1건 | 200(400 아님) |
| readiness | 14 §4 `/health/ready` | 200 `HEALTHY` |
| 예산 | 14 U/B 비율 | 경고 없음(≤ 1.05) |
| 24h | 다음날 `14 --compare` 다시 | 오류 비율·비용 급변 없음 |

## 롤백

- **코드+DB 전부** — (3) 스냅샷 복원(새 클러스터로 생기므로 RDS Proxy 대상 교체가 따른다, 1h+) **+** `helm rollback`([8-U 4단계](8-U-update.md#4단계--안-되면-되돌린다)). 코드만 되돌리면 옛 이미지가 마이그레이션 0032 가 심은 행을 못 읽어 모델 목록이 깨진다.
- **단가만** — `snapshots/<ts>-08-pricing-rollback.sql` 을 psql 파드로.
- **values 태그만** — `snapshots/<ts>-values-dev.yaml.bak` 복원 후 `install-eks.sh dev`.

## (10) prod — prod 계정의 배포 EC2 에서, dev (9) 를 하루 지켜본 뒤

dev 와 같은 순서다. 다른 것은 셋뿐 — values 파일 이름, 스냅샷·terraform 디렉터리의 `prod`, 스크립트 인자 `prod`. `config.env` 는 prod EC2 의 것(`DEPLOY_ENV="prod"`)을 쓴다.

**(10-1) 저장소 최신화**

▶ 실행
```bash
cd ~/awsome-ai-gateway && git remote -v
V=deployment/charts/llm-gateway/values-eks-fargate-prod.yaml
cp $V ~/values.bak && git fetch origin
git reset --hard origin/us/deploy-fixes && cp ~/values.bak $V
cmp -s $V ~/values.bak && echo "values restored OK" || echo "RESTORE FAILED"
```

**(10-2) 사전 점검 · 설정 적용**

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
grep -n '^DEPLOY_ENV' config.env
bash 00-preflight-check.sh
bash 14-postdeploy-check.sh --save pre
bash 06-persist-annotations.sh
bash 15-set-master-secret-ref.sh
bash 17-set-websearch-caps.sh
bash 17-set-websearch-caps.sh --apply
```
기대: `DEPLOY_ENV="prod"` · 「4. Migration pre-check」 전부 OK · `06` 은 `already matches` · `15`·`17` 은 `nothing to do`(아니면 `--apply`).

**(10-3) DB 스냅샷**

▶ 실행
```bash
SNAP=llm-gateway-prod-pre-sync-$(date +%Y%m%d)
aws rds create-db-cluster-snapshot --db-cluster-identifier llm-gateway-prod \
  --db-cluster-snapshot-identifier $SNAP --query DBClusterSnapshot.Status
aws rds wait db-cluster-snapshot-available --db-cluster-snapshot-identifier $SNAP
aws rds describe-db-cluster-snapshots --db-cluster-snapshot-identifier $SNAP \
  --query 'DBClusterSnapshots[0].[DBClusterSnapshotIdentifier,Status]' --output text
```
기대: 마지막 줄 `llm-gateway-prod-pre-sync-<날짜>  available`.

**(10-4) terraform — 인프라 변경 확인**

▶ 실행
```bash
cd ~/awsome-ai-gateway/deployment/terraform/environments/llm-gateway-prod
terraform init
terraform plan -no-color -detailed-exitcode > ~/plan.txt 2>&1; echo "exit=$?"
grep -E '# .* (will be|must be)|^Plan:|No changes|Error' ~/plan.txt
```
판정·apply·멈출 때는 (4) 와 같습니다.

**(10-5) 태그 올림**

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 13-bump-image-tags.sh prod
bash 13-bump-image-tags.sh prod --apply
```

**(10-6) 이미지 6개 빌드·push**

▶ 실행
```bash
cd ~/awsome-ai-gateway
for s in migration gateway-proxy admin-api admin-ui notification-worker \
  cost-recorder-worker; do ./deployment/scripts/rebuild-image.sh $s prod || break; done
```

**(10-7) 배포**

▶ 실행
```bash
cd ~/awsome-ai-gateway/deployment/terraform/environments/llm-gateway-prod
terraform init | tail -3
terraform output -json >/dev/null && echo "output OK"
cd ~/awsome-ai-gateway && ./deployment/scripts/install-eks.sh prod
```

**(10-8) 단가 · 시드 alias**

▶ 실행
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 08-set-model-pricing.sh
bash 08-set-model-pricing.sh --apply
```
그다음 admin UI › Models 에서 시드 alias INACTIVE(8단계와 같음).

**(10-9) 사후 점검** — 순서·기대·기능별 확인 표는 (9) 와 같습니다.

▶ 실행 · prod EC2 — 자동 점검과 접속값
```bash
cd ~/awsome-ai-gateway/docs/us-llm-gateway/update-scripts
bash 14-postdeploy-check.sh --compare snapshots/pre.numbers
bash 07-client-values.sh
```
▶ 실행 · Mac — VK 받기: (9) 의 Mac 단계를 prod EC2 의 07 출력 4줄로 합니다.

▶ 실행 · prod EC2 — 종단 1건
```bash
GW=https://$(kubectl -n llm-gateway get ingress llm-gateway-gateway -o jsonpath='{.spec.rules[0].host}')
bash 04-verify.sh --base-url $GW --vk <VK>
```

롤백은 §롤백에서 `dev`→`prod`(스냅샷 `llm-gateway-prod-pre-sync-…`, 백업 `snapshots/<ts>-values-prod.yaml.bak`).
