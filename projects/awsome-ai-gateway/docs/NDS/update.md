# 업데이트 · 롤백

모든 변경은 `gateway.yaml` → `render` → 기동 → `doctor` 순서로 합니다.
환경에 따라 별도 스크립트를 찾아 돌릴 필요가 없습니다.

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
> 확인하고 직접 채우세요. `eks` 캡처는 아직 미구현입니다.

## 일반 업데이트 (기능 on/off, 설정 변경)

```bash
vi deployment/gateway.yaml        # 변경
./deploy validate                 # 검증
./deploy render                   # 산출물 재생성 — 시크릿은 보존됨
docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d --build
./deploy doctor                   # ✓ 확인
```

`doctor` 가 `.env` drift, 서비스 이상, 마이그레이션 head 불일치를 보고하면
해당 메시지대로 조치하세요.

## 코드 업데이트 (새 버전 배포)

```bash
cd awsome-ai-gateway
git fetch && git checkout <새 브랜치/태그>
./deploy render
docker compose --env-file deployment/gen/<env>/.env -f deployment/gen/<env>/docker-compose.yml up -d --build
./deploy doctor
```

마이그레이션은 `migration` 서비스가 기동 시 자동으로 `alembic upgrade head`
까지 올립니다 — 앱보다 먼저 완료되도록 depends_on으로 순서가 보장됩니다.

## 롤백

plane 별로 다릅니다 — "태그만 되돌리기"는 롤백이 아닙니다:

| 무엇이 바뀌었나 | 되돌리는 법 |
|---|---|
| 앱 코드/이미지 | 이전 커밋/태그로 `render` + `up -d` |
| 설정(gateway.yaml) | 이전 값으로 되돌려 `render` + 재기동 |
| **DB 마이그레이션** | 다운그레이드 없음 — forward-fix (새 마이그레이션으로 수복) |
| `.env` 삭제/유실 | **백업에서 복원** — VIRTUAL_KEY_ENCRYPTION_KEY를 잃으면 발급된 Virtual Key 전부 재발급 필요 |

## 도메인을 나중에 얻었을 때

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
