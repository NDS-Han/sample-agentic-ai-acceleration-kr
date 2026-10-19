# 배포 호스트 준비 — Deployment EC2 생성 → 도구 설치 → 세션 재시작

> **한 줄**: 랩톱에는 VS Code + Remote-SSH만 깔면 된다 — AWS 명령과 도구는
> **전부 배포 EC2에서** 돈다. 이 문서는 어느 리전에서든 같은 절차입니다
> (us-llm-gateway install-guide 의 리전 중립 버전).

## 0. 개념

```
랩톱 (VS Code + Remote-SSH — 화면/키보드만)
   └── ssh ──▶ 배포 EC2 (Ubuntu, deployer IAM role)
                  ├── aws-cli/terraform/kubectl/helm/docker/psql
                  ├── git clone → docs/NDS/deploy
                  └── AWS 계정에 인프라 생성 (compose/ecs/eks)
```

랩톱에 aws-cli는 **필요 없습니다** — 모든 AWS 명령은 배포 EC2(아래 §3에서 설치)
또는 콘솔 CloudShell에서 돕니다.

## 1. Deployer IAM role — EC2 띄우기 **전에** (콘솔 CloudShell)

배포 EC2는 VPC·EKS·RDS·ElastiCache·Cognito·IAM·ECR·Secrets Manager·Lambda
등을 만들 수 있는 권한이 필요합니다. IAM은 글로벌이라 `--region` 불필요.

```bash
ROLE=llm-gateway-deployer

# ① 신뢰 정책 — 이 역할을 EC2 서비스가 맡을 수 있게
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

# ② 역할 생성 + 관리형 정책 2개
aws iam create-role --role-name "$ROLE" \
  --assume-role-policy-document file:///tmp/ec2-trust.json
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
aws iam attach-role-policy --role-name "$ROLE" \
  --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore

# ③ instance profile — ⚠️ 콘솔은 자동 생성하지만 CLI는 안 만든다.
#    이 두 줄을 빼먹으면 시작 마법사의 IAM instance profile 목록에 안 뜬다.
aws iam create-instance-profile --instance-profile-name "$ROLE"
aws iam add-role-to-instance-profile \
  --instance-profile-name "$ROLE" --role-name "$ROLE"

# ④ ARN 메모 — eks 경로면 terraform tfvars 의 principal_arn 에 이 값을 쓴다
aws iam get-role --role-name "$ROLE" --query Role.Arn --output text
```

> `AdministratorAccess` 대신 최소권한으로 가려면 배포 backend 별 필요 권한을
> 먼저 정해야 합니다 — 평가/초기 구축은 관리형 정책이 현실적입니다.

## 2. EC2 생성

- **리전**: 배포할 리전과 같은 리전 권장 (terraform state·ECR·Bedrock 호출 지연)
- **AMI**: Ubuntu Server LTS (**x86_64**) — 22.04/24.04/26.04 확인됨
- **타입**: **t3.xlarge 이상** — 이미지 빌드 6종 + terraform apply 가 버겁다
- **스토리지**: gp3 **128GB+** (이미지 빌드 캐시)
- **네트워크**: Auto-assign public IP + SSH 허용 — ⚠️ 보안그룹 SSH를
  `0.0.0.0/0`으로 두지 말고 **작업자 IP `/32`** 로 좁힐 것
- **Advanced details ▸ IAM instance profile = `llm-gateway-deployer`**

이미 띄운 EC2에 나중에 붙여도 됩니다 (재시작 불필요):

```bash
aws ec2 associate-iam-instance-profile --region <배포 리전> \
  --instance-id <i-xxxx> --iam-instance-profile Name=llm-gateway-deployer
```

**접속 + 역할 확인** (aws-cli는 §3에서 설치되므로 IMDS 직접 조회):

```bash
ssh -i ~/.ssh/<키>.pem ubuntu@<EC2 퍼블릭 IP>

TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 60")
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/iam/info
# → "InstanceProfileArn": "...:instance-profile/llm-gateway-deployer" 면 OK
```

## 3. Bedrock 모델 액세스 — 먼저 확인, 대개 불필요

2025-10부터 serverless 모델은 자동 활성화됩니다. **Anthropic 모델만** 최초
호출 전 use case form이 **계정당 1회** 필요합니다 — 이미 냈으면 건너뜁니다.

▶ **실행** · CloudShell 또는 배포 EC2 (aws-cli 설치 후) — `<배포 리전>` 채우기

```bash
for m in global.anthropic.claude-opus-4-5 \
         global.anthropic.claude-sonnet-4-5 \
         global.anthropic.claude-haiku-4-5-20251001-v1:0; do
  s=$(aws bedrock get-foundation-model-availability \
    --region <배포 리전> --model-id "$m" \
    --query authorizationStatus --output text \
    --no-cli-pager | tr -d '\r')
  printf '%-55s %s\n' "$m" "$s"
done
# → 모두 AUTHORIZED 면 OK
```

> NDS 기본 모델은 **`global.*` inference profile** — 리전 중립이라 어느
> 리전에서 배포해도 같은 ID를 씁니다. 해당 리전에 `global.` 프로파일이
> 없으면 사용하는 배포 리전의 프로파일 프리픽스(`us.`/`eu.`/`apac.`)로 바꿔 확인.
> AUTHORIZED가 아니면 콘솔 **Bedrock ▸ Model catalog**(배포 리전) → Anthropic
> 모델 아무거나 → **use case form 제출**(website URL 필요, 즉시 승인) — 1회로
> 전 모델이 열립니다.

## 4. 저장소 + `bootstrap-ec2.sh` — 도구 한 번에 설치

▶ **실행** · 배포 EC2

```bash
git clone <repo-url> ~/repo && cd ~/repo
git checkout nds/dev
cd projects/awsome-ai-gateway
bash deployment/scripts/bootstrap-ec2.sh
```

스크립트가 **멱등**으로 설치하고 버전을 검증합니다 (실패 시 `❌ MISSING`과
함께 중단):

| 도구 | 요구 | 비고 |
|---|---|---|
| aws-cli | v2 | |
| terraform | ≥ 1.9 | |
| kubectl | EKS ±1 minor (`KUBECTL_VERSION`으로 pin) | eks 경로 |
| helm | 3.x (≥ 3.14 권장) | eks 경로 |
| docker + buildx | ≥ 24 | §ECR 이미지 빌드 — buildx 필수(optional-glob COPY) |
| jq / yq / psql 16 / git / tmux | — | psql은 `PG_MAJOR`로 pin |
| python3-venv/pip | — | `docs/NDS/deploy` 도구 venv 용 |
| Claude Code | — | Bedrock pin (`AWS_REGION`, `ANTHROPIC_MODEL_PREFIX`로 override) |

> env override: `AWS_REGION` (기본 us-west-2) · `KUBECTL_VERSION` ·
> `PG_MAJOR` · `ANTHROPIC_MODEL_PREFIX` (기본 `us.anthropic` — 배포 리전의
> 프로파일 프리픽스로)

## 5. 세션 재시작 — 건너뛰면 docker가 막힙니다

bootstrap이 바꾼 것(docker 그룹·PATH)은 **지금 셸에 반영되지 않습니다** —
그룹 자격은 세션 생성 시점에 박히므로 `usermod`만으론 떠 있는 셸이 안 바뀝니다.

▶ **실행** · 배포 EC2 — 어떤 IDE 서버가 떠 있는지 먼저 확인

```bash
pgrep -af 'vscode-server|cursor-server|windsurf-server|kiro-server|code-server|remote-dev'
```

```bash
pkill -f '<위에서 확인된 서버명>'    # 예: vscode-server, cursor-server, windsurf-server …
```

→ IDE별 원격 서버 디렉토리: VS Code `~/.vscode-server` · Cursor `~/.cursor-server` ·
Windsurf `~/.windsurf-server` · Kiro `~/.kiro-server` · JetBrains `~/.cache/JetBrains`.
서버가 죽으면 IDE에 "Reload"가 뜨고 새 세션으로 다시 붙습니다 — bootstrap 종료
메시지가 **감지된 서버에 맞는 pkill을 자동으로 출력**하므로 그대로 따라 해도 됩니다.
원격 IDE 없이 순수 SSH면 그냥 접속을 끊고 재접속하면 됩니다.

▶ **새 셸에서 확인**

```bash
echo "export AWS_DEFAULT_REGION=<배포 리전>" >> ~/.bashrc
export AWS_DEFAULT_REGION=<배포 리전>
docker ps && aws sts get-caller-identity
# arn:aws:sts::<계정>:assumed-role/llm-gateway-deployer/<i-xxxx> 면 준비 완료
```

## 6. 다음

deploy 도구 venv 만들고 backend를 고릅니다:

```bash
cd ~/repo/projects/awsome-ai-gateway
python3 -m venv docs/NDS/.venv
docs/NDS/.venv/bin/pip install -r docs/NDS/deploy/requirements.txt
```

| backend | 문서 |
|---|---|
| compose (평가·소규모) | [install-compose.md](install-compose.md) |
| ecs (표준 권장) | [install-ecs.md](install-ecs.md) |
| eks (대규모·신규/기존) | [install-eks.md](install-eks.md) |
