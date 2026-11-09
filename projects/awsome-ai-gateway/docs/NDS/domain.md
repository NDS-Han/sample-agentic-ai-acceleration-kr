# 도메인 — 모드 선택, 획득, 등록

`gateway.yaml`의 `domain:` 블록 하나로 HTTPS와 주소 체계를 선언합니다.

```yaml
domain:
  mode: none | selfsigned-temp | route53-acm | cloudfront-temp
  name: example.com        # route53-acm 일 때
  zone_id: Z0123...        # Route53 hosted zone — 있으면 인증서·레코드도 자동
```

| mode | 무엇 | 용도 |
|---|---|---|
| `none` | HTTP만 — host 없이 ALB/IP 직접 접근 | 로컬·테스트. **Cowork 불가**(https만 받음), Cognito SSO 불가 |
| `selfsigned-temp` | self-signed 인증서로 **admin-ui만** 임시 https (EKS) | 도메인 없이 Cognito SSO가 필요할 때 — 브라우저 경고 있음, gateway는 HTTP 유지 |
| `route53-acm` | 정식 도메인 + ACM 인증서 + DNS | 정식 배포 — 3개 호스트 전부 https |
| `cloudfront-temp` | CloudFront 기본 도메인 임시 https | EKS 전용 (미구현 스텁) |

## 1. 도메인 획득 — 사람 절차 (NDS 범위 밖)

도메인 등록·hosted zone 생성·사내 DNS 요청은 조직 절차라 도구가 다루지 않습니다.
획득 경로는 셋 중 하나:

- **경로 A — Route53에서 구매/이전**: 가장 간단. Registrar로 등록하면 hosted
  zone·NS 위임 자동 → `zone_id`만 채우면 전부 자동.
- **경로 B — 외부 등록기관 도메인을 Route53으로 NS 위임**: hosted zone만 Route53에
  만들고 등록기관에서 NS를 바꾸면 경로 A와 동일한 자동 경로.

  ```bash
  aws route53 create-hosted-zone --name example.com --caller-reference "$(date +%s)"
  # → NS 4개를 등록기관 콘솔에 입력 → dig NS example.com +short 로 위임 확인
  ```

- **경로 C — 사내 DNS에서 서브도메인 할당**: hosted zone이 없으므로
  `zone_id`는 비웁니다 — 인증서 검증 CNAME·host 레코드가 매번 수동입니다.

## 2. 도메인 등록 — `route53-acm` (업데이트)

`gateway.yaml`에 mode/name/zone_id를 채우고 `./deploy apply`하면 됩니다 —
신규든 `none`에서 전환이든 같은 경로입니다.

```yaml
domain:
  mode: route53-acm
  name: ssirlabs.net
  zone_id: Z06161782VTHCXZ98NZPG   # Route53 zone — 없으면 비워두고 수동 레코드
```

> **클라이언트 안내 필수** — 주소가 `https://gateway.<domain>` 등으로 바뀝니다.
> `nextauthUrl`·Cognito 콜백은 apply가 갱신하지만 **기존 로그인 세션은 끊기고**
> 새 주소를 다시 안내해야 합니다 (`./deploy client-values`).

### backend별 apply 내부 동작

| backend | apply가 자동으로 하는 것 | 사람이 하는 것 |
|---|---|---|
| **compose** | Caddy가 3개 호스트 라우팅 + Let's Encrypt 자동 발급 | 3개 호스트 → 서버 IP 레코드 **먼저** 등록 (없으면 cert 발급 실패) |
| **ecs** | ACM wildcard cert → DNS 검증 → 443 리스너 → Route53 alias → Cognito 콜백 | `zone_id` 채우기만 하면 끝 (경로 A/B) |
| **eks** | 인증서 보장 → 인그레스 host+443 → Route53 A alias → Cognito 콜백 → `nextauthUrl` | `zone_id` 채우기만 하면 끝 — 없으면 아래 §2-1 |

### eks에서 apply가 하는 일 (상세)

전부 멱등입니다 — 이미 있으면 찾아 쓰고, 미완성이면 다음 apply에 이어서 합니다:

1. **인증서 보장** — ACM에 `*.<domain>` 발급 인증서가 없으면 요청 → `zone_id`
   zone에 검증 CNAME 자동 등록 → `ISSUED` 대기 (~3분; 넘으면 다음 apply에 부착)
2. **리스너** — 인그레스 3개에 `listen-ports: 80+443` + `ssl-redirect` 선언,
   `certificate-arn`은 `--set`으로 주입 (env values에 손댈 것 없음)
3. **DNS** — `gateway./admin./admin-api.<domain>` A alias를 zone에 upsert
   (live ALB 주소로 — ALB 재생성돼도 다음 apply가 따라감)
4. **Cognito 콜백** — gen tfvars에 `cognito_callback_urls`/`cognito_logout_urls`를
   **env tfvars 기존 목록과 병합해** 기록 → 콜백 미등록 감지 시
   `terraform apply -var-file=… -target=module.cognito` 자동 실행
5. **`nextauthUrl = https://admin.<domain>`** 자동

### 2-1. `zone_id`가 없을 때 (경로 C)

인증서와 레코드만 수동입니다 — apply는 발급된 인증서를 찾아 쓰고 미등록을
안내합니다:

1. ACM에 `*.<domain>` + `<domain>` 인증서 요청 (DNS 검증) → 사내 DNS에 검증
   CNAME 등록 → `ISSUED`
2. `gateway./admin./admin-api.<domain>` 각각 → 대응 ALB로 CNAME (또는 alias)
3. `./deploy apply` 재실행 → 발급된 cert를 찾아 부착 + 콜백·nextauthUrl 반영

### host 이름 규칙

`domain.name`을 쓰면 host는 **env 무관** `gateway./admin./admin-api.<domain>`으로
고정됩니다. `gateway-dev.<domain>` 같은 기존 명명을 유지해야 하면 `domain.name`을
비워두세요 — overlay가 host를 건드리지 않아 env values(eks)/Caddyfile(compose)의
이름이 유지됩니다 (채택 배포 판단은 [update.md](update.md) 온보딩 절 참조).

## 3. 도메인 없이 임시 HTTPS — `selfsigned-temp` (EKS 전용)

도메인이 없어도 **admin-ui Cognito SSO 로그인**이 필요하면(콜백 URL이 https
강제) 이 모드를 씁니다 — `configure` 마법사 도메인 질문에서도 고를 수 있습니다.

apply가 하는 일(전부 자동·멱등):

1. admin-ui ALB hostname으로 **self-signed 인증서 생성** (openssl,
   CN=`llm-gateway-<env>-selfsigned` — 이미 있으면 재사용)
2. **ACM에 import** 후 `certificate-arn`을 `--set`으로 주입
3. ingress에 `listen-ports: 80+443` + `ssl-redirect` 선언
4. `nextauthUrl`을 `https://<alb>`로 주입

이후 사람이 하는 것 — **Cognito 콜백은 terraform env 소유**라 아래 파일에 등록
후 `terraform apply`합니다:

```
deployment/terraform/environments/<env명>/terraform.tfvars
# 예: deployment/terraform/environments/llm-gateway-prod/terraform.tfvars
```

```hcl
cognito_callback_urls = [ ..., "https://<admin-ui ALB>/api/auth/callback" ]
cognito_logout_urls   = [ ..., "https://<admin-ui ALB>/" ]
```

```bash
cd deployment/terraform/environments/<env명>
terraform apply    # aws_cognito_user_pool_client.cli 1건 update 만 나와야 정상
```

주의:

- **브라우저 경고가 뜹니다** — self-signed라 신뢰 체인이 없습니다. Cognito는
  스킴만 검사해 로그인은 정상 동작합니다. 사내 PKI로 서명해 올리면 경고도 없음.
- **gateway는 여전히 HTTP** — Cowork는 안 됩니다. admin-ui SSO 전용 임시 장치.
- 첫 설치처럼 ALB가 아직 없으면 인증서 없이 지나가고, ALB 생성 후 다음 apply에
  자동으로 붙습니다.
- **도메인 도입 시**: `mode: route53-acm`으로 바꾸기만 하면 됩니다 — apply가
  정식 인증서·레코드를 붙이고, import한 self-signed는
  `aws acm delete-certificate`로 정리하세요.

## 4. Cognito 콜백과 AD 연동

- 콜백/로그아웃 URL은 **terraform env 소유**입니다 — `route53-acm`이면 apply가
  gen tfvars로 갱신하지만, 수동 변경이 필요하면 env의 `terraform.tfvars`에서
  `cognito_callback_urls`/`cognito_logout_urls`를 고치고 `terraform apply`합니다.
- SAML(ADFS) 연동으로 사내 AD 로그인을 붙이는 절차는 [ad-sync.md](ad-sync.md).
