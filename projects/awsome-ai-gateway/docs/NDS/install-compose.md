# t0 설치 — EC2 1대에 Docker Compose로 배포 (실행 런북)

> **대상**: 평가·POC·소규모(~50명) 또는 다른 AWS 계정에서의 배포 테스트.
> **방법**: 위에서 아래로 순서대로 실행합니다. 각 절 첫 줄의 "한 줄" 요약만 봐도 흐름이 잡힙니다.

**완성되면**: PostgreSQL + Redis + 마이그레이션 + 앱 6개(gateway-proxy·admin-api·admin-ui·scheduler·cost-recorder-worker·notification-worker) + Caddy(유일한 인입점)

> ⚠️ **알고 시작하세요** — t0는 단일 노드입니다. 서버가 죽으면 서비스가 멈추고 복구는 백업에서 합니다(§6). 100명 이상·운영 중요도가 있으면 [install-ecs.md](install-ecs.md)(t1)를 선택하세요.

---

## 1. 사전 준비

### 1-1. 작업자 환경 (랩톱)

> **한 줄**: 랩톱에는 SSH 클라이언트만 있으면 됩니다 — 모든 명령은 배포 EC2 안에서 돌립니다.

- **VS Code + Remote-SSH 확장**을 쓰면 터미널과 파일 편집을 한 화면에서 할 수 있습니다(선택). 터미널 ssh만으로도 충분합니다.
- **SSH 클라이언트**: Mac은 내장. Windows는 PowerShell에서 `ssh -V`로 확인 — 없으면 *설정 ▸ 앱 ▸ 선택적 기능 ▸ OpenSSH 클라이언트* 설치.
- 랩톱에 aws-cli는 **필요 없습니다** — AWS 명령은 전부 배포 EC2 또는 콘솔 CloudShell에서 돌립니다.

### 1-2. 배포용 EC2 만들기

> **한 줄**: 액세스 키를 파일에 두는 대신 **IAM instance role**로 인증합니다(임시 자격증명 자동 순환 — 키 유출 경로 자체를 없앰).

**사양**

| 항목 | 값 |
|---|---|
| AMI | Ubuntu LTS x86_64 (22.04/24.04/26.04) |
| 타입 | `t3.xlarge` 이상 (4 vCPU·16GB — 이미지 6개 빌드 필요) |
| 스토리지 | gp3 **40GB+** (이미지 빌드·로그 여유로 128GB 권장) |
| 리전 | 사용할 리전 — 모델이 `global.*` 프로파일이라 리전에 덜 민감 |

**① IAM 역할 만들기** — EC2를 띄우기 **전에** 만들어야 시작 마법사 목록에 뜹니다.
AWS 콘솔 **CloudShell**(왼쪽 하단)에서 실행합니다. IAM은 글로벌이라 리전 무관:

▶ **실행** · AWS 콘솔 CloudShell

```bash
ROLE=llm-gateway-host

# ① 신뢰 정책 — EC2 서비스가 이 역할을 맡을 수 있게
cat > /tmp/ec2-trust.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Principal": { "Service": "ec2.amazonaws.com" },
    "Action": "sts:AssumeRole"
  }]
}
EOF

# ② Bedrock 호출만 허용하는 최소권한 정책 (앱 서버는 인프라 생성이 아니라
#    모델 호출만 하므로 AdministratorAccess 는 필요 없습니다)
cat > /tmp/bedrock-policy.json <<'EOF'
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"],
    "Resource": "*"
  }]
}
EOF

aws iam create-role --role-name "$ROLE" \
  --assume-role-policy-document file:///tmp/ec2-trust.json
aws iam put-role-policy --role-name "$ROLE" \
  --policy-name bedrock-invoke --policy-document file:///tmp/bedrock-policy.json
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# ③ instance profile — EC2가 붙이는 건 역할이 아니라 이것.
#    (이 두 줄을 빼먹으면 시작 마법사 목록에 안 뜹니다)
aws iam create-instance-profile --instance-profile-name "$ROLE"
aws iam add-role-to-instance-profile \
  --instance-profile-name "$ROLE" --role-name "$ROLE"
```

> 💡 **SES 알림을 쓸 예정이면** 위 정책의 `Action` 배열에 `"ses:SendEmail"`, `"ses:SendRawEmail"`을 추가하세요(§4에서 provider=ses 선택 시).

**② EC2 시작** — 시작 마법사에서:

- 이름 `llm-gateway-t0` · AMI Ubuntu LTS · 타입 `t3.xlarge` · **Key pair** 선택(없으면 새로 생성해 `.pem` 다운로드)
- 스토리지 gp3 40GB+
- **Advanced details ▸ IAM instance profile = `llm-gateway-host`**
- Security group:

| 포트 | 소스 | 용도 |
|---|---|---|
| 22 | 작업자 랩톱 공인 IP `/32` | SSH |
| 8000 | 사용자 PC 대역 (또는 `allowed_cidrs`) | 게이트웨이 (도메인 없을 때) |
| 8080 | 관리자 대역 | Admin API (도메인 없을 때) |
| 3000 | 관리자 대역 | Admin UI (도메인 없을 때) |
| 80, 443 | 사용자 대역 | 도메인(route53-acm)을 쓸 때 |

> ⚠️ SSH를 `0.0.0.0/0`으로 열지 마세요 — 랩톱 공인 IP `/32`로 좁히세요.
> 이미 띄운 EC2라면 프로파일을 나중에 붙여도 됩니다(재시작 불필요):
> `aws ec2 associate-iam-instance-profile --instance-id <i-xxxx> --iam-instance-profile Name=llm-gateway-host`

**③ 접속 + 역할 확인** — EC2 안에서 IMDS를 두드려 역할 부착을 확인:

▶ **실행** · 배포 EC2

```bash
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/security-credentials/
# → llm-gateway-host 가 나오면 성공
```

### 1-3. Bedrock 모델 액세스 확인

> **한 줄**: Anthropic 모델은 최초 호출 전 use case form이 **계정당 1회** 필요합니다 — 대개 이미 돼 있으니 확인만 합니다.

다른 계정에서 처음 배포하는 경우 이 절이 특히 중요합니다. aws-cli가 아직 없으면 콘솔 CloudShell에서:

▶ **실행** · CloudShell (리전 = 배포할 리전)

```bash
aws bedrock get-foundation-model-availability \
  --region <배포리전> --model-id global.anthropic.claude-sonnet-4-5 \
  --query authorizationStatus --output text
# → AUTHORIZED 면 OK. 아니면 콘솔 Bedrock ▸ Model catalog 에서
#   Anthropic 모델 하나를 골라 use case form 을 1회 제출 (즉시 승인, 3모델 모두 열림)
```

### 1-4. 도구 설치

> **한 줄**: 도구 설치는 `bootstrap-ec2.sh`가 한 번에 합니다 — 손으로 깔 건 없습니다.

먼저 저장소를 받고(스크립트가 그 안에 있습니다) 부트스트랩을 돌립니다:

▶ **실행** · 배포 EC2

```bash
cd ~
git clone <이 저장소> sample-agentic-ai-acceleration-kr
ln -s ~/sample-agentic-ai-acceleration-kr/projects/awsome-ai-gateway ~/awsome-ai-gateway
cd ~/awsome-ai-gateway
bash deployment/scripts/bootstrap-ec2.sh   # git·docker+buildx·aws-cli·jq 등 + 버전 검증
```

> compose 경로에 terraform/kubectl/helm은 안 쓰지만 스크립트가 함께 깔아도 해롭지 않습니다.
> **docker-buildx + BuildKit**이 이 스크립트가 설치하는 핵심입니다 — 기본 docker.io만으로는 일부 Dockerfile이 빌드되지 않습니다.

**새 셸에서 마무리** — bootstrap이 추가한 docker 그룹은 현재 셸에 반영되지 않습니다:

▶ **실행** · 배포 EC2

```bash
pkill -f 'vscode-server|cursor-server'   # VS Code/ Cursor 로 붙어있다면 Reload 후 새 세션
# 또는 그냥 SSH 재접속

docker ps && aws sts get-caller-identity
# → 컨테이너 목록(비어도 OK) + assumed-role/llm-gateway-host ARN 이 나오면 준비 완료
```

---

## 2. 설정 만들기 — `./deploy init` (또는 `configure`)

> **한 줄**: 질문에 답하면 `deployment/gateway.yaml`(이 배포의 유일한 설정 원본)이 생깁니다.

▶ **실행** · 배포 EC2

```bash
cd ~/awsome-ai-gateway
python3 -m venv docs/NDS/.venv
docs/NDS/.venv/bin/pip install -r docs/NDS/deploy/requirements.txt
./deploy init
```

질문 흐름(Enter = 기본값):

```
환경 이름: my-gw
AWS 리전: <배포 리전>
배포 대상: compose
규모 티어: t0
HTTPS 도메인: none            ← 도메인 없으면 none (나중에 추가 가능)
알림 provider: mock           ← 이메일 발송 필요하면 ses/smtp
추가 기능: (기본 전부 off — 다른 계정 테스트면 전부 off 권장)
OIDC 로그인: n                ← 다른 계정에 Cognito 없으면 n (dev-login으로 접속)
접근 허용 CIDR: <사용자 PC 대역>
```

> ⚠️ `allowed_cidrs`를 비우면 **인터넷 전체에 게이트웨이가 열립니다** — 반드시 대역을 넣으세요.

## 3. 배포 — `./deploy apply`

> **한 줄**: render(산출물 생성) → 확인 → `docker compose up -d` 까지 한 명령입니다.

▶ **실행** · 배포 EC2

```bash
./deploy apply --plan    # 무엇을 만들지 먼저 확인 (아무것도 안 바뀜)
./deploy apply           # 확인 프롬프트 y → 빌드+기동 (첫 빌드 10~20분)
```

끝나면 doctor가 자동 실행됩니다 — 전부 ✓이면 설치 완료:

```
✓ [OK  ] secrets: 필수 시크릿 존재
✓ [OK  ] services: postgres healthy / migration 완료 / gateway-proxy healthy ...
✓ [OK  ] drift: .env 가 gateway.yaml 과 일치
```

> ⚠️ `deployment/gen/<env>/.env`에는 DB 비밀번호와 `VIRTUAL_KEY_ENCRYPTION_KEY`가
> 자동 생성돼 들어갑니다(권한 0600). **이 파일을 잃으면 발급된 Virtual Key가 전부
> 무효화됩니다** — 값을 비밀번호 관리자에 별도 보관하세요(§6).

## 4. 접속 확인

`domain.mode: none`으로 설치했다면 포트로 접속:

| 서비스 | 주소 |
|---|---|
| 게이트웨이 (Claude Code/Codex가 쓸 주소) | `http://<EC2 퍼블릭 IP>:8000` |
| Admin API | `http://<EC2 퍼블릭 IP>:8080` |
| Admin UI | `http://<EC2 퍼블릭 IP>:3000` — OIDC를 끄고 설치했으면 dev-login 버튼 |

**테스트**: 게이트웨이에 요청이 가는지 — `curl http://<IP>:8000/health`

> Cowork(Claude Desktop)는 `https://`만 받습니다 — `domain.mode: none`이면
> Cowork는 동작하지 않습니다(Claude Code·Codex는 됨). 도메인을 나중에 얻으면
> [update.md](update.md)의 도메인 추가 절로 전환하세요.

## 5. 이후 운영

```bash
./deploy configure     # 설정 변경 — 항목별 확인 → 끝에서 배포까지
./deploy doctor        # 상태·드리프트 점검
./deploy apply --plan  # 변경 미리보기
```

## 6. 백업 (중요 — 단일 노드라 백업이 전부입니다)

▶ **실행** · 배포 EC2 — cron 등록 예시

```bash
mkdir -p ~/backups
# 매일 02:00, DB 덤프 + .env 사본
(crontab -l; echo '0 2 * * * docker compose --env-file ~/awsome-ai-gateway/deployment/gen/<env>/.env \
  -f ~/awsome-ai-gateway/deployment/gen/<env>/docker-compose.yml \
  exec -T postgres pg_dump -U gateway gateway | gzip > ~/backups/gateway-$(date +\%F).sql.gz') | crontab -
cp ~/awsome-ai-gateway/deployment/gen/<env>/.env ~/backups/env-$(date +%F).bak
```

---

## 문제가 생기면

| 증상 | 확인 |
|---|---|
| `docker ps` permission denied | bootstrap 후 재로그인 안 함 → SSH 재접속 |
| 이미지 빌드 실패 (COPY … no source files) | buildx 미설치 → `bootstrap-ec2.sh`가 BuildKit을 켭니다. 재실행 |
| 컨테이너가 안 뜸 | `docker compose -f gen/<env>/docker-compose.yml logs <서비스>` |
| Bedrock 4xx | §1-3 모델 액세스 + §1-2 instance role 확인. `docker compose logs gateway-proxy` |
| 로그인 안 됨 | OIDC 4개 값 확인 — hosted-ui authorize/token URL은 issuer가 아님 |
| 클라이언트 403 | 사용자 PC 공인 IP가 `allowed_cidrs`와 SG 양쪽에 있는지 |
| `deploy` 명령 에러 | 메시지에 다음 행동이 같이 나옵니다 — 안내를 그대로 따르세요 |
