# 업데이트 · 롤백

모든 변경은 `gateway.yaml` → `render` → 기동 → `doctor` 순서로 합니다.
환경에 따라 별도 스크립트를 찾아 돌릴 필요가 없습니다.

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

## public → private 전환

단일 플래그 전환이 아니라 **순서 있는 작업**입니다:

1. 사용자가 게이트웨이에 도달할 경로를 먼저 확보 (VPN/DX/사내망)
2. `network.allowed_cidrs` 를 사내 대역으로 좁히고 `render` + 재기동
3. Caddy 가 허용 대역 외 접속을 403으로 차단 (SG로도 이중 차단 권장)
4. `network.mode: private` 로 표시 후 인스턴스를 프라이빗 서브넷으로 이전 — 이 단계는 서버 재배치라 계획된 다운타임 필요
