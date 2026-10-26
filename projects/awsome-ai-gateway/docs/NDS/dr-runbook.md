# DR 런북 — 백업 복구·시크릿 유실·재해 복구

> **목적**: LLM Gateway가 통째로 사라졌을 때(또는 부분이 죽었을 때) 어디서 무엇을
> 복구하는지를 백엔드별로 적어둔 절차서. 평소에는 §0의 "백업 확인 체크리스트"만
> 주기적으로 보면 됩니다.

## 0. 무엇이 재해에서 살아남는가

| 자산 | 어디 있나 | 유실 시 |
|---|---|---|
| **DB** (모델/사용자/VK/사용량/예산/audit) | compose: EC2 로컬 pgdata 볼륨 · ecs/eks: Aurora 클러스터 | **치명적** — 아래 §2·§3 절차로 복구 |
| `.env` / `deployment/gateway.yaml` | compose: 배포 EC2 `deployment/gen/<env>/.env` · ecs/eks: 배포 호스트 + Secrets Manager | 재생성 가능하나 **VK 암호키는 예외** |
| `VIRTUAL_KEY_ENCRYPTION_KEY` | `.env` 안 (eks는 Secrets Manager 경유) | **유실 시 발급된 VK 전부 복호화 불가 → §4.1** |
| 이미지 | ECR 리포지토리 | 그대로 남음 — 재배포는 태그 재지정뿐 |
| Redis/Valkey (캐시) | ElastiCache / compose 컨테이너 | 휘발성 — 재시작 후 자연 재구성, 복구 불필요 |
| Helm 릴리스 / k8s 리소스 | eks 클러스터 | `./deploy apply`로 재렌더·재적용 |
| Cognito 사용자 풀 | terraform 상태 + AWS 리전 | 유실은 드묾 — §4.3 |

## 1. RTO / RPO 기준

| 백엔드 | RPO(데이터 손실 상한) | RTO(복구 목표) | 근거 |
|---|---|---|---|
| compose (t0) | **24h** — pg_dump 크론 주기 | 수 시간 — 새 EC2에 재설치+복원 | install-compose.md §6 |
| ecs / eks (t1+) | **~5분** — Aurora 연속 백업(PITR) | 30–60분 — PITR → 엔드포인트 전환 | aurora-postgresql 모듈 `backup_retention_period`(dev 7일, prod 14일) |

> 단, 이 수치는 **백업이 실제로 존재할 때**만 성립합니다 — §5 리허설로 검증하세요.

## 2. 백엔드별 복구 절차

### 2.1 compose — 서버 소실 / DB 손상

전제: `~/backups/`에 최신 `gateway-YYYY-MM-DD.sql.gz`와 `env-*.bak`이 있거나,
오프박스 사본(S3 등)이 있을 것. **EC2가 통째로 사라지면 로컬 백업도 같이 사라진다 —
주기적 오프박스 복사가 이 런북의 전제다.**

```bash
# 1) 새 EC2에 스택 재설치 — install-compose.md 절차를 gateway.yaml 기준으로 그대로
./deploy install   # 또는 bootstrap — 환경에 맞는 NDS 명령

# 2) 백업 .env 복원 — 최소한 이 두 줄이 맞아야 나머지가 말이 된다
#    VIRTUAL_KEY_ENCRYPTION_KEY / DATABASE_URL / OIDC_* — 없으면 §4
cp env-<날짜>.bak deployment/gen/<env>/.env

# 3) DB 복원
gunzip -c gateway-<날짜>.sql.gz | \
  docker compose -f deployment/gen/<env>/docker-compose.yml \
    exec -T postgres psql -U gateway -d gateway -v ON_ERROR_STOP=1

# 4) 재기동 + 스모크
docker compose -f deployment/gen/<env>/docker-compose.yml up -d
deployment/scripts/smoke-test.sh   # 또는 ./deploy doctor
```

### 2.2 eks / ecs — Aurora 장애·데이터 손상

Aurora는 PITR(Point-in-Time Restore)로 **새 클러스터**를 만들고 엔드포인트를 갈아끼우는
방식입니다 — 원위치 덮어쓰기가 아니므로 롤백 여유가 있습니다.

```bash
ENV=dev   # 또는 prod
TF=deployment/terraform/environments/llm-gateway-$ENV   # ecs는 gateway-ecs

# 1) 손상 시점 직전 시각을 UTC로 확정 (예: 2026-10-26T01:00:00Z)

# 2) PITR — 새 클러스터 생성 (identifier 는 자유, 원본 클러스터 식별자는 terraform output)
aws rds restore-db-cluster-to-point-in-time \
  --region <region> \
  --db-cluster-identifier llm-gateway-$ENV-restored \
  --source-db-cluster-identifier $(terraform -chdir=$TF output -raw aurora_cluster_id) \
  --restore-to-time 2026-10-26T01:00:00Z \
  --restore-type copy-on-write

# 3) 인스턴스 부착 (클러스터만으로는 뜨지 않는다)
aws rds create-db-instance --region <region> \
  --db-instance-identifier llm-gateway-$ENV-restored-0 \
  --db-cluster-identifier llm-gateway-$ENV-restored \
  --db-instance-class db.serverless --engine aurora-postgresql
# (serverless는 min/max capacity를 클러스터에 맞춰야 할 수 있음 — terraform 모듈 참조)

# 4) 새 writer 엔드포인트 확인
aws rds describe-db-clusters --region <region> \
  --db-cluster-identifier llm-gateway-$ENV-restored \
  --query 'DBClusters[0].Endpoint' --output text
```

엔드포인트 전환 — 앱은 DB 주소를 **시크릿**으로 읽습니다:

```bash
# eks/ecs 공통 — Secrets Manager의 db 시크릿 안 host 값을 새 엔드포인트로 교체
aws secretsmanager get-secret-value --secret-id /llm-gateway/$ENV/db --query SecretString --output text | jq .
aws secretsmanager update-secret --secret-id /llm-gateway/$ENV/db \
  --secret-string "$(<기존 JSON에 host만 교체>)"

# eks — 파드 재시작으로 external-secrets→env 재주입
kubectl -n llm-gateway rollout restart deploy llm-gateway-admin-api \
  llm-gateway-gateway-proxy llm-gateway-scheduler \
  llm-gateway-notification-worker llm-gateway-cost-recorder-worker

# ecs — 서비스 재배포(시크릿 재해석)
aws ecs update-service --cluster <cluster> --service <svc> --force-new-deployment
```

> ⚠️ RDS Proxy를 쓰는 배포면 프록시 `target`도 새 클러스터로 바꿔야 합니다 —
> `terraform -chdir=$TF output` 에 `rds_proxy_*` 출력이 있는지 확인하고,
> 있으면 프록시 target group 수정(또는 모듈 변수 `cluster_identifier`를 새
> 클러스터로 임시 전환 후 apply).

**스냅샷에서 복구** (PITR 윈도 밖 / 최종 스냅샷): 위 절차의 2번을
`restore-db-cluster-from-snapshot --snapshot-identifier <snapshot>` 으로 교체.
prod는 삭제 시 final snapshot(`<project>-<env>-final-*`)이 자동 남습니다.

### 2.3 전체 리전 소실

문서상 지원 범위 밖 — 리전 간 복제가 없습니다. 필요하면 Aurora Global Database +
multi-region 배포가 선결 작업입니다(현행 설계는 단일 리전).

## 3. Helm/애플리케이션 복구 (DB는 무사한 경우)

```bash
# eks — 릴리스가 지워졌거나 엉켰을 때. gateway.yaml 이 정본이면:
./deploy apply            # declarative 재적용
./deploy doctor           # drift/누락 점검

# 진짜 처음부터인데 DB만 살아있는 케이스:
./deploy bootstrap        # 인프라+앱 전체 — Aurora는 terraform이 재사용/재생성
```

## 4. 시크릿 유실 시나리오

### 4.1 `VIRTUAL_KEY_ENCRYPTION_KEY` 유실 — VK 전멸

이 키는 VK를 AES-256-GCM으로 암호화하는 키입니다. **유실하면 DB에 남은 암호문을
복호화할 방법이 없습니다** — `admin.list_keys`/게이트웨이 인증이 전부 깨집니다.

복구 = 재암호화가 아니라 **전원 재발급**입니다:

```bash
# 1) 새 키 생성·설치 — .env (compose) 또는 Secrets Manager (eks/ecs)
openssl rand -hex 32    # 새 VIRTUAL_KEY_ENCRYPTION_KEY
#    eks: /llm-gateway/<env>/app 시크릿 등 실제 저장 위치에 반영 후 파드 재시작

# 2) 기존 VK 전부 폐기 (admin API)
#    GET /admin/keys 로 목록 → POST /admin/keys/{id}/revoke
#    또는 운영 SQL: UPDATE auth.virtual_keys SET status='REVOKED' WHERE status='ACTIVE';

# 3) 사용자에게 새 VK 발급 안내 — /keys 화면에서 각자 재발급 (또는 관리자가 일괄 발급)
```

> **예방이 전부**: 이 키는 백업 위치를 두 군데 이상(팀 비밀금고 + 배포 호스트 백업)으로
> 하세요. operations.md §2 체크리스트의 "`.env`/시크릿 보관" 항목이 바로 이것입니다.

### 4.2 `.env` 전체 유실

- `gateway.yaml`과 배포 호스트의 `deployment/gen/<env>/`은 `./deploy doctor --capture`로
  라이브 환경에서 다시 캡처할 수 있습니다.
- 캡처로 안 되는 값(시크릿 평문): DB 비밀번호는 Secrets Manager/콘솔에서 확인,
  `VIRTUAL_KEY_ENCRYPTION_KEY`는 §4.1, OIDC client secret은 Cognito에서 재발급.

### 4.3 Cognito 사용자 풀 유실/손상

- 풀 자체는 terraform(`modules/cognito`)이 소유 — `terraform apply`로 재생성되지만
  **사용자/그룹/속성은 terraform 밖의 데이터**라 날아갑니다.
- 복구 = 사용자 재동기: Cognito 재생성 후 operations.md §1의 그룹 매핑/사용자 등록
  절차를 다시 수행. admin-ui의 Cognito sync가 그룹→팀 매핑을 재구성합니다.
- VK는 내부 저장소라 Cognito와 무관하게 살아남습니다(재로그인 후 그대로 동작).

## 5. 복구 리허설 체크리스트 (분기 1회 권장)

백업은 **복원해본 것만** 믿을 수 있습니다.

- [ ] compose: 최신 `gateway-*.sql.gz`를 스테이징 postgres에 실제로 restore 해봄
- [ ] eks/ecs: PITR을 **버리는 클러스터**로 한 번 실행 — 시간과 절차를 실측
- [ ] 복원 DB에서 행 확인: `select count(*) from auth.users;` / `model.model_aliases`
- [ ] `VIRTUAL_KEY_ENCRYPTION_KEY` 사본이 두 곳에 살아있는지 확인
- [ ] `deployment/gateway.yaml` + `.env`가 리포지토리 외 백업 위치에 최신인지
- [ ] 복구 후 스모크: `deployment/scripts/smoke-test.sh` / `./deploy doctor`
