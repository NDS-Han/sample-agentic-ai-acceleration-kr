# 도메인 획득과 등록

이 도구는 **도메인 획득 자체는 다루지 않습니다**(등록기관·hosted zone 생성·
사내 DNS 요청은 조직마다 다른 사람 절차). NDS가 하는 일은 획득한 도메인을
이미 배포된 환경에 **선언 변경 + 업데이트**로 붙이는 것까지입니다.

```
① 도메인 없이 배포 (domain.mode: none)
        │
② 도메인 획득 — 사람 절차, 이 문서 §1        ← NDS 범위 밖
        │
③ 배포 환경에 등록 — gateway.yaml 수정 + apply  ← NDS 범위, 이 문서 §2
```

> 참고: `domain.mode: none`이면 HTTP만 열립니다. Claude Code·Codex는 되지만
> **Cowork(Claude Desktop)는 https:// 만 받아 동작하지 않습니다** — 도메인은
> 사실상 필수로 생각하세요.

## 1. 도메인 획득 — 세 경로

### 경로 A. AWS(Route53)에서 도메인 구매/이전

가장 손이 적게 갑니다 — Route53 Registrar로 등록하면 hosted zone이 자동
생성되고 NS 위임도 자동입니다.

```bash
# 콘솔: Route53 → Registered domains → Register domain
# 완료 후 hosted zone id 확인 (ecs 의 domain.zone_id 입력값)
aws route53 list-hosted-zones-by-name --dns-name example.com \
  --query 'HostedZones[0].Id' --output text   # /hostedzone/Z0... 형태 → Z0... 부분
```

### 경로 B. 외부에서 산 도메인을 Route53 hosted zone으로 NS 위임

가비아·Cloudflare 등 다른 등록기관 도메인도, hosted zone만 Route53에 만들어
위임하면 **ecs 의 완전 자동 경로**(cert 발급·검증·alias)를 그대로 씁니다.

```bash
# 1. hosted zone 생성 → NS 4개 확인
aws route53 create-hosted-zone --name example.com \
  --caller-reference "$(date +%s)" --output text
aws route53 list-resource-record-sets --hosted-zone-id <Z0...> \
  --query "ResourceRecordSets[?Type=='NS'].ResourceRecords[].Value" --output text

# 2. 도메인 등록기관 콘솔에서 NS 레코드를 위 4개로 변경
# 3. 위임 확인 (전파까지 수분~수시간)
dig NS example.com +short
```

### 경로 C. 사내 도메인 시스템에서 서브도메인 할당

회사 DNS 팀에서 `llm.example.corp` 같은 서브도메인을 할당받는 경우.
**hosted zone이 없으므로** DNS 레코드 등록·갱신은 매번 사내 절차를 탑니다 —
ecs도 이 경우 zone_id 없이 수동 검증 경로로 갑니다(§2 참조). 사내 DNS에
필요한 레코드는:

- ACM 인증서 **검증용 CNAME**(cert 발급 시 1회성 — `_xxx.<domain>` 형태)
- 서비스 호스트 3개의 **CNAME → ALB DNS**(ecs/eks) 또는 **A레코드 → 서버 IP**(compose)

## 2. 획득한 도메인을 배포에 붙이기 (업데이트)

공통 흐름 — `domain.mode: none`에서 전환도 일반 업데이트와 동일한 경로입니다:

```bash
# gateway.yaml
domain:
  mode: route53-acm
  name: example.com
  zone_id: Z0123...       # ecs 전용 — Route53 hosted zone (경로 A/B 한정)

./deploy configure        # 또는 파일 직접 편집 후
./deploy apply            # render 내장 — plan 미리보기 → 적용
./deploy doctor           # endpoint/TLS 확인
```

> **클라이언트 안내 필수** — 게이트웨이 주소가 `http://<ip>:8000` 등에서
> `https://gateway.example.com`으로 바뀝니다. `adminUi.nextauthUrl`과 Cognito
> 콜백은 apply가 갱신하지만 **기존 로그인 세션은 끊기고**, 직원들에게 새 주소를
> 다시 안내해야 합니다 (`./deploy client-values`로 재발급 출력).

backend 별로 apply 안에서 일어나는 일이 다릅니다:

| backend | apply가 자동으로 하는 것 | 사람이 하는 것 |
|---|---|---|
| **compose** | Caddy가 `gateway./admin-api./admin.<domain>` 라우팅 + Let's Encrypt 자동 발급 (ACM 불요 — 어떤 DNS든 됨) | 도메인 제공자에서 3개 호스트 → 서버 IP 레코드 **먼저** 등록 (없으면 인증서 발급 실패) |
| **ecs** | ACM wildcard cert 발급 → DNS 검증 레코드 → 443 리스너 → `gateway./admin./admin-api.` Route53 alias → Cognito 콜백 | `zone_id` 채우기만 하면 끝 (경로 A/B) |
| **eks** | ingress host 3개(`gateway./admin./admin-api.<domain>`) + `nextauthUrl` 갱신 | ACM 발급·검증 → env values의 `certificate-arn` → apply 후 Route53/사내 DNS에 CNAME 3개 — [install-eks.md](install-eks.md) §4 |

### 사내 도메인(zone_id 없음)으로 ecs를 쓰는 경우

`domain.name`만 넣고 `zone_id`는 비우세요 — 모듈이 cert를 만들되 검증
레코드를 만들 수 없으니 `cert_validation_records` output으로 내보냅니다.
그 레코드를 사내 DNS에 등록 → cert가 `ISSUED`되면 `certificate_arn`을
tfvars/`infra` 대신 gen tfvars에 추가해 재apply하면 HTTPS가 열립니다.
자세한 순서는 [install-ecs.md](install-ecs.md) §6.

### host 이름 규칙 — 레거시/사내 명명과 다를 수 있음

`domain.name`을 쓰면 host는 **env 무관** `gateway./admin./admin-api.<domain>`
규칙으로 고정됩니다. `gateway-dev.<domain>` 같은 기존 명명을 유지해야 하면
`domain.name`을 비워두세요 — overlay가 host를 건드리지 않아 env
values(eks)/Caddyfile(compose)의 기존 이름이 유지됩니다. 자세한 판단은
[update.md](update.md)의 온보딩 절 참조.
