# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""deploy bootstrap — 신규 설치 오케스트레이션 (install-guide §3 전체 자동화).

경계:
  apply     — 기존 인프라 위의 업데이트/동기화 (helm upgrade + feature 인프라 보강)
  bootstrap — 아무것도 없는 계정에서 첫 배포까지. 각 단계는 멱등이라
              중간 실패 후 다시 실행하면 이어간다.

eks 순서 (install-guide §3-1 → §3-8 과 동일):
  1. env scaffold    — terraform env 디렉토리가 없으면 템플릿에서 생성
  2. tfstate         — bootstrap-tfstate.sh 로 state bucket + lock table 보장
  3. env apply       — terraform apply (EKS·Aurora·ElastiCache·Cognito·Secrets·IRSA)
  4. secrets         — /<project>/<env>/app(랜덤 4키) · /redis 복사 · /db 검증 (§3-4)
  5. images          — ECR repo 생성 + build/push (§3-5)
  6. helm apply      — cmd_apply 와 같은 eks_apply.apply
  7. cognito         — adminBootstrap.emails 의 첫 사용자를 풀에 생성 (§3-8)

ecs 순서: tfstate 보장 → cmd_apply (terraform 이 나머지 전부 생성).
compose: 신규=업데이트 동일 — bootstrap 은 곧 apply.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path

import yaml

from .schema import GatewayConfig
from . import eks_apply, websearch  # noqa: F401  (websearch는 apply 경로에서 사용)

ENV_TEMPLATE = "llm-gateway-dev"   # scaffold 원본 — 모듈 배선이 검증된 env
TF_FILES = ("backend.tf", "main.tf", "outputs.tf", "providers.tf", "variables.tf")

# §3-5 — helm 이 당기는 서비스 6종. scheduler 는 admin-api 이미지 재사용.
# (helm_key, repo 이름, 빌드 컨텍스트)
EKS_SERVICES = (
    ("gatewayProxy", "gateway-proxy", "gateway-proxy"),
    ("adminApi", "admin-api", "admin-api"),
    ("adminUi", "admin-ui", "admin-ui"),
    ("notificationWorker", "notification-worker", "notification-worker"),
    ("costRecorderWorker", "cost-recorder-worker", "cost-recorder-worker"),
    ("migration", "migration", "db"),
)


def _run(argv: list[str], *, cwd: Path | None = None, env: dict | None = None,
         capture: bool = False) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(a) for a in argv)}", file=sys.stderr)
    try:
        r = subprocess.run([str(a) for a in argv], cwd=cwd, env=env,
                           capture_output=capture, text=True)
    except FileNotFoundError:
        raise SystemExit(f"{argv[0]} 명령을 못 찾았습니다 — 설치/PATH 확인 필요")
    if r.returncode != 0:
        if capture:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
        raise SystemExit(f"명령 실패 ({r.returncode}): {' '.join(str(a) for a in argv)}")
    return r


def _aws(args: list[str], region: str, *, capture: bool = True,
         ok_rc: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    r = subprocess.run(["aws", *args, "--region", region, "--output", "json"],
                       capture_output=True, text=True)
    if r.returncode not in ok_rc:
        tail = (r.stderr or "").strip().splitlines()
        raise SystemExit(f"aws {' '.join(args[:2])} 실패 — "
                         f"{tail[-1] if tail else r.returncode}")
    return r


def account_id(region: str) -> str:
    r = _aws(["sts", "get-caller-identity", "--query", "Account"], region)
    # --query + --output json 은 "1234.." (따옴표 포함) — json 로 벗긴다
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return r.stdout.strip().strip('"')


def region_code(region: str) -> str:
    """aws 리전 → 짧은 코드 (Cognito suffix/이름 생성용).
    us-east-1→use1, ap-northeast-2→apne2, eu-central-1→euc1, ap-southeast-2→apse2.
    복합 방향(northeast/southeast)은 두 글자, 단일 방향/중앙은 한 글자."""
    parts = region.split("-")
    if len(parts) < 3:
        return region.replace("-", "")
    # 방향어 → 나침반 축약 ("northeast"[:2]="no" 가 아니라 "ne" 이어야 한다)
    compass = {"north": "n", "northeast": "ne", "east": "e", "southeast": "se",
               "south": "s", "southwest": "sw", "west": "w", "northwest": "nw",
               "central": "c"}
    mid = "".join(compass.get(p, p[0]) for p in parts[1:-1] if p)
    return parts[0] + mid + parts[-1]


def default_cognito_suffix(region: str, account: str) -> str:
    return f"{region_code(region)}-auth-{account}"


def list_azs(region: str) -> list[str]:
    r = _aws(["ec2", "describe-availability-zones",
              "--query", "AvailabilityZones[].ZoneName"], region)
    return sorted(json.loads(r.stdout))


# ── 1. env scaffold ───────────────────────────────────────────────────────────

def env_dir(cfg: GatewayConfig) -> str:
    return cfg.deploy.tf_env_dir or \
        f"deployment/terraform/environments/llm-gateway-{cfg.env}"


def scaffold_tfvars(example: str, *, env: str, region: str, account: str,
                    principal_arn: str) -> str:
    """terraform.tfvars.example → 신규 env 의 terraform.tfvars.

    org 특화 블록(cognito_groups/tags/chat agent)은 템플릿 기본값을 그대로 두고
    검토 주석을 단다 — 잘못된 자동값보다 사람 검토가 낫다."""
    t = example
    t = t.replace('environment = "dev"', f'environment = "{env}"')
    t = t.replace('aws_region  = "ap-northeast-2"', f'aws_region  = "{region}"')
    t = t.replace('cognito_domain_suffix = "vanilla-auth-CHANGE_ACCOUNT_ID"',
                  f'cognito_domain_suffix = '
                  f'"{default_cognito_suffix(region, account)}"')
    t = t.replace('principal_arn = "arn:aws:iam::ACCOUNT_ID:role/YOUR_ROLE"',
                  f'principal_arn = "{principal_arn}"')
    # NDS 배포는 admin-chat-agent(BI 챗)를 포함하지 않는다 — example 은 본
    # deliverable 운영 환경 재현용으로 true 라 신규 설치에선 꺼 둔다
    t = t.replace("enable_chat_agent    = true", "enable_chat_agent    = false")
    t = t.replace("enable_chat_db_tools = true", "enable_chat_db_tools = false")
    header = (
        "# generated by `deploy bootstrap` — terraform.tfvars.example 에서 생성.\n"
        "# ⚠️ 검토 필요: eks_cluster_version/addon_versions(최신 권장으로), "
        "cognito_groups(조직 그룹),\n"
        "#   tags, enable_chat_agent(불필요하면 false). 그 외는 배포 계정 실측값으로 채워짐.\n\n")
    return header + t


def scaffold_eks_env(cfg: GatewayConfig, repo_root: Path, region: str) -> Path:
    """deploy.tf_env_dir 또는 기본 경로의 env 디렉토리 — 없으면 템플릿에서 생성."""
    target = repo_root / env_dir(cfg)
    if target.is_dir():
        return target
    template = repo_root / "deployment/terraform/environments" / ENV_TEMPLATE
    if not template.is_dir():
        raise SystemExit(f"env 템플릿 {template} 이 없습니다 — "
                         "deploy.tf_env_dir 로 기존 env 를 지정하십시오")
    target.mkdir(parents=True)
    for f in TF_FILES:
        (target / f).write_text((template / f).read_text())
    caller = _aws(["sts", "get-caller-identity"], region)
    arn = json.loads(caller.stdout).get("Arn", "")
    example = (template / "terraform.tfvars.example").read_text()
    (target / "terraform.tfvars").write_text(scaffold_tfvars(
        example, env=cfg.env, region=region,
        account=account_id(region), principal_arn=arn))
    print(f"ⓘ terraform env 를 새로 만들었습니다: {target}\n"
          "  terraform.tfvars 의 주석 표시된 항목(cluster 버전·cognito_groups·tags)을 "
          "검토하세요 — 계속 진행하려면 bootstrap 을 다시 실행합니다.",
          file=sys.stderr)

    # env values 파일도 없으면 dev 를 복사 — 없으면 ESO·adminBootstrap·ALB
    # 어노테이션이 차트 기본값이라 helm 이 시크릿 참조 실패로 뜨지 못하고
    # ADMIN_GROUPS 도 비어 관리자가 아무도 못 된다.
    chart_dir = repo_root / "deployment/charts/llm-gateway"
    vals_dst = chart_dir / f"values-eks-fargate-{cfg.env}.yaml"
    if not vals_dst.exists() and not (
            chart_dir / f"values-eks-fargate-{cfg.env}.local.yaml").exists():
        src = chart_dir / "values-eks-fargate-dev.yaml"
        if src.exists():
            vals_dst.write_text(src.read_text())
            print(f"ⓘ env values 도 {src.name} 에서 복사했습니다: {vals_dst.name}\n"
                  "  adminBootstrap.emails 를 실제 운영자 이메일로 바꾸세요 — "
                  "그대로 두면 admin@example.com 사용자가 생성됩니다.",
                  file=sys.stderr)
    raise SystemExit(f"{target} 생성 — tfvars 검토 후 bootstrap 재실행")


# ── 2. tfstate ────────────────────────────────────────────────────────────────

def default_tfstate_names(cfg: GatewayConfig, *, eks: bool) -> tuple[str, str]:
    """backend 별 기본 state 이름 — eks env 관례(-vanilla-)와 ecs render 의
    backend.hcl 추론 규칙이 다르므로 섞으면 init 이 엉뚱한 버킷을 본다."""
    acct = cfg.aws.account_id or account_id(cfg.aws.region)
    if eks:
        return (cfg.deploy.tfstate_bucket or f"llm-gateway-vanilla-tfstate-{acct}",
                cfg.deploy.tfstate_table or "llm-gateway-vanilla-tflock")
    return (cfg.deploy.tfstate_bucket or f"llm-gateway-tfstate-{acct}",
            cfg.deploy.tfstate_table or "llm-gateway-vanilla-tflock")


def ensure_tfstate(cfg: GatewayConfig, repo_root: Path, *, eks: bool,
                   bucket: str = "", table: str = "") -> tuple[str, str]:
    """bootstrap-tfstate.sh 로 state bucket + lock table 보장 (멱등).

    반환 (bucket, table) — eks env init 의 -backend-config 인자로 쓴다."""
    def_bucket, def_table = default_tfstate_names(cfg, eks=eks)
    bucket = bucket or def_bucket
    table = table or def_table
    env = dict(os.environ, AWS_REGION=cfg.aws.region,
               TFSTATE_BUCKET=bucket, TFLOCK_TABLE=table)
    _run(["bash", "deployment/scripts/bootstrap-tfstate.sh"],
         cwd=repo_root, env=env)
    if eks and not cfg.deploy.tfstate_table:
        print(f"ⓘ lock table {table} 를 만들었습니다 — gateway.yaml 의 "
              "deploy.tfstate_table 에도 같은 이름을 선언하면 backend.hcl/init "
              "인자와 일치합니다", file=sys.stderr)
    return bucket, table


# ── 3. env terraform apply ────────────────────────────────────────────────────

def env_apply(env_dir_path: Path, gen_dir: Path,
              backend: tuple[str, str] | None = None) -> dict:
    """terraform init(+backend-config) → apply (base tfvars + managed overlay).

    반환: terraform outputs."""
    init = ["terraform", "init", "-input=false"]
    if backend and not (env_dir_path / ".terraform").exists():
        bucket, table = backend
        init += [f"-backend-config=bucket={bucket}",
                 f"-backend-config=dynamodb_table={table}"]
    _run(init, cwd=env_dir_path)
    var_args = ["-var-file=terraform.tfvars"]
    managed = gen_dir / "terraform.tfvars"
    if managed.exists():
        # cwd 는 env_dir 라 상대경로는 절대경로로 변환해 넘긴다
        var_args.append(f"-var-file={managed.resolve()}")  # 뒤에 오는 -var-file 이 이긴다
    _run(["terraform", "apply", "-auto-approve", "-input=false", *var_args],
         cwd=env_dir_path)
    return eks_apply.tf_outputs(env_dir_path)


# ── 4. 시크릿 시드 (§3-4) ────────────────────────────────────────────────────

def secret_prefix(cfg: GatewayConfig, repo_root: Path) -> str:
    """chart 의 externalSecrets.secretPathPrefix — env overlay 우선."""
    from .render import eks as eks_render
    vf = eks_render.env_values_file(cfg, repo_root)
    if vf:
        try:
            vals = yaml.safe_load(vf.read_text()) or {}
            p = ((vals.get("externalSecrets") or {}).get("secretPathPrefix"))
            if p:
                return str(p)
        except yaml.YAMLError:
            pass
    return "/llm-gateway/"


def seed_secrets(cfg: GatewayConfig, repo_root: Path, outs: dict) -> list[str]:
    """<prefix><env>/app(랜덤 4키) · /redis(auth_token 복사) 생성, /db 검증.

    없을 때만 만든다 — 기존 시크릿은 절대 덮어쓰지 않는다(VK/세션 무효화 방지).
    반환: 상태 note 목록."""
    prefix = secret_prefix(cfg, repo_root)
    region = cfg.aws.region
    notes: list[str] = []

    def exists(name: str) -> bool:
        r = subprocess.run(
            ["aws", "secretsmanager", "describe-secret", "--secret-id", name,
             "--region", region, "--output", "json"],
            capture_output=True, text=True)
        return r.returncode == 0

    app = f"{prefix}{cfg.env}/app"
    if exists(app):
        notes.append(f"secrets {app} — 이미 존재 (보존)")
    else:
        app_secret = json.dumps({
            "virtual_key_encryption_key": secrets.token_hex(32),
            "nextauth_secret": secrets.token_hex(32),
            "jwt_jwks_cache_key": secrets.token_hex(32),
            # R3-1 — secret.yaml 의 remoteRef 가 이 키를 요구한다 (없으면 ES sync 실패)
            "internal_api_token": secrets.token_hex(32),
        })
        _aws(["secretsmanager", "create-secret", "--name", app,
              "--secret-string", app_secret], region)
        notes.append(f"secrets {app} — 생성 (랜덤 4키)")

    redis = f"{prefix}{cfg.env}/redis"
    if exists(redis):
        notes.append(f"secrets {redis} — 이미 존재 (보존)")
    else:
        arn = outs.get("elasticache_auth_token_secret_arn")
        if not arn:
            raise SystemExit(
                "terraform output elasticache_auth_token_secret_arn 이 없습니다 — "
                f"{redis} 를 만들 수 없습니다 (env apply 선행 확인)")
        pw = _aws(["secretsmanager", "get-secret-value", "--secret-id", arn,
                   "--query", "SecretString"], region)
        token = json.loads(pw.stdout)
        _aws(["secretsmanager", "create-secret", "--name", redis,
              "--secret-string", json.dumps({"password": token})], region)
        notes.append(f"secrets {redis} — auth_token 복사 완료")

    db = f"{prefix}{cfg.env}/db"
    r = subprocess.run(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", db,
         "--query", "SecretString", "--region", region, "--output", "text"],
        capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit(
            f"secrets {db} 가 없습니다 — terraform env apply 가 만들어야 하는 "
            "시크릿입니다. env apply 결과를 확인하십시오 (절대 수동 create 하지 말 "
            "것 — master_password 를 잃는다)")
    keys = set(json.loads(r.stdout).keys())
    if not {"password", "master_password"} <= keys:
        raise SystemExit(
            f"secrets {db} 의 키가 불완전합니다({sorted(keys)}) — password 와 "
            "master_password 둘 다 필요합니다. terraform apply 로 복구하십시오 "
            "(put-secret-value 로 손대지 말 것)")
    notes.append(f"secrets {db} — terraform 관리본 확인 (password+master_password)")
    return notes


# ── 5. 이미지 (§3-5) ─────────────────────────────────────────────────────────

def ensure_ecr_repos(region: str, services: tuple = EKS_SERVICES) -> None:
    for _, repo, _ in services:
        r = subprocess.run(
            ["aws", "ecr", "create-repository", "--repository-name",
             f"llm-gateway/{repo}",
             "--image-scanning-configuration", "scanOnPush=true",
             "--encryption-configuration", "encryptionType=AES256",
             "--region", region],
            capture_output=True, text=True)
        if r.returncode != 0 and "RepositoryAlreadyExists" not in r.stderr:
            raise SystemExit(f"ECR repo 생성 실패 (llm-gateway/{repo}): "
                             f"{(r.stderr or '').strip()}")


def push_images_eks(cfg: GatewayConfig, outs: dict, repo_root: Path,
                    region: str) -> None:
    """ECR repo 보장 → docker login → 서비스별 build+push.

    태그: images.tags[svc] → images.tag — render 와 같은 순서라 helm 이
    당기는 태그와 항상 일치한다."""
    acct = cfg.aws.account_id or account_id(region)
    registry = (cfg.images.registry
                or f"{acct}.dkr.ecr.{region}.amazonaws.com").rstrip("/")
    ensure_ecr_repos(region)
    r = subprocess.run(["aws", "ecr", "get-login-password", "--region", region],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise SystemExit("ECR 로그인 비밀번호 조회 실패 — 자격증명 확인")
    lr = subprocess.run(["docker", "login", "--username", "AWS",
                         "--password-stdin", registry],
                        input=r.stdout, capture_output=True, text=True)
    if lr.returncode != 0:
        raise SystemExit(f"docker login 실패: {(lr.stderr or '').strip()}")

    for helm_key, repo, ctx in EKS_SERVICES:
        tag = (cfg.images.tags or {}).get(helm_key) or cfg.images.tag
        if not tag:
            raise SystemExit("images.tag (또는 images.tags 전체 핀)이 필요합니다 — "
                             "빌드할 태그를 gateway.yaml 에 선언하십시오")
        image = f"{registry}/llm-gateway/{repo}:{tag}"
        _run(["docker", "build", "--platform", "linux/amd64",
              "-t", image, str(repo_root / ctx)], cwd=repo_root)
        _run(["docker", "push", image], cwd=repo_root)


# ── 7. Cognito 온보딩 (§3-8) ─────────────────────────────────────────────────

def onboard_admin(cfg: GatewayConfig, repo_root: Path, outs: dict,
                  region: str) -> list[str]:
    """adminBootstrap.emails[0] 사용자를 Cognito 풀에 생성 + ClaudeAdmin 배정.

    임시 비밀번호는 생성해서 한 번만 출력한다 — 첫 로그인 시 변경 강제.
    사용자가 이미 있으면 생성 생략(멱등). 반환: 상태 note 목록."""
    notes: list[str] = []
    pool = outs.get("cognito_user_pool_id")
    if not pool:
        notes.append("cognito_user_pool_id output 없음 — 사용자 온보딩 생략 "
                     "(terraform env 가 Cognito 를 안 만들었거나 output 이름 확인 필요)")
        return notes
    from .render import eks as eks_render
    emails: list[str] = []
    vf = eks_render.env_values_file(cfg, repo_root)
    if vf:
        try:
            vals = yaml.safe_load(vf.read_text()) or {}
            emails = [str(e) for e in
                      ((vals.get("adminApi") or {}).get("adminBootstrap") or {})
                      .get("emails") or []]
        except yaml.YAMLError:
            pass
    if not emails:
        notes.append("adminApi.adminBootstrap.emails 가 env values 에 없습니다 — "
                     "Cognito 사용자는 수동 생성 (ops 8-Y)")
        return notes
    email = emails[0]
    r = subprocess.run(
        ["aws", "cognito-idp", "admin-get-user", "--user-pool-id", pool,
         "--username", email, "--region", region],
        capture_output=True, text=True)
    if r.returncode != 0:
        temp_pw = secrets.token_urlsafe(16) + "Aa1!"
        _aws(["cognito-idp", "admin-create-user", "--user-pool-id", pool,
              "--username", email,
              "--user-attributes", f"Name=email,Value={email}",
              "Name=email_verified,Value=true",
              "--temporary-password", temp_pw,
              "--message-action", "SUPPRESS"], region)
        notes.append(f"cognito 사용자 생성: {email} — 임시 비밀번호(1회 표시, "
                     f"첫 로그인 시 변경 강제): {temp_pw}")
    else:
        notes.append(f"cognito 사용자 {email} — 이미 존재 (생성 생략)")
    teams = _cognito_team_groups(pool, region)
    if not teams:
        notes.append("⚠ 팀 그룹(Claude_*)이 풀에 없습니다 — 팀 그룹 소속이 없으면 "
                     "이 관리자도 VK 발급 시 403 이 납니다. infra.cognito_groups/"
                     "env tfvars 에 팀 그룹을 선언했는지 확인하세요")
    for group in ("ClaudeAdmin", *teams):
        gr = subprocess.run(
            ["aws", "cognito-idp", "admin-add-user-to-group", "--user-pool-id",
             pool, "--username", email, "--group-name", group,
             "--region", region],
            capture_output=True, text=True)
        if gr.returncode == 0:
            notes.append(f"cognito 그룹 배정: {email} → {group}")
        else:
            notes.append(f"⚠ {group} 그룹 배정 실패 — 그룹이 풀에 있는지 확인 "
                         f"({(gr.stderr or '').strip()[:120]})")
    return notes


def _cognito_team_groups(pool: str, region: str) -> list[str]:
    """VK 발급은 팀 그룹(Claude_*) 멤버십을 요구한다 — 첫 관리자도 예외가 아니라
    실제 풀의 Claude_* 그룹 중 ClaudeAdmin 을 뺀 첫 것에 배정한다.
    라이브 풀을 읽으므로 ecs(infra.cognito_groups)든 eks(env tfvars)든
    선언 출처와 무관하게 실제 그룹을 고른다."""
    r = subprocess.run(
        ["aws", "cognito-idp", "list-groups", "--user-pool-id", pool,
         "--region", region, "--output", "json"],
        capture_output=True, text=True)
    if r.returncode != 0:
        return []
    try:
        names = [g.get("GroupName", "")
                 for g in json.loads(r.stdout).get("Groups", [])]
    except json.JSONDecodeError:
        return []
    teams = sorted(n for n in names
                   if n.startswith("Claude_") and n != "ClaudeAdmin")
    return teams[:1]


# ── 신규 설치 대화형 wizard ───────────────────────────────────────────────────

def wizard_infra(cfg: GatewayConfig, *, eks: bool) -> dict | None:
    """신규 설치 인프라 질문 — install-guide §3-1/§3-2 를 대화형으로.

    반환 None = 사용자 취소. Enter 기본값은 gateway.yaml 기존 선언/규약 기본.
    답변은 cmd_bootstrap 이 yaml 에 영속화한다 — 재실행·업데이트 시 같은
    값이 managed overlay/generated tfvars 로 계속 내려간다."""
    import questionary
    from questionary import Choice

    def ask(*a, **k):
        v = questionary.text(*a, **k).ask()
        if v is None:
            return None
        return v

    out: dict = {"infra": {}}

    # ── ① terraform state 백엔드
    def_bucket, def_table = default_tfstate_names(cfg, eks=eks)
    print("\n■ 1/4 — terraform state 백엔드 (S3 bucket + DynamoDB lock table)")
    bucket = ask("  TFSTATE_BUCKET", default=def_bucket)
    if bucket is None:
        return None
    table = ask("  TFLOCK_TABLE", default=def_table)
    if table is None:
        return None
    out["tfstate"] = (bucket.strip() or def_bucket, table.strip() or def_table)

    # ── ② 가용영역 (DB/ElastiCache subnet group 요건 — 2개 이상)
    print("\n■ 2/4 — 가용영역 (subnet cidr 은 개수에 맞춰 자동 생성)")
    try:
        azs = list_azs(cfg.aws.region)
    except SystemExit:
        azs = []
        print("  ⚠ AZ 목록 조회 실패 — 리전/권한 확인 후 수동 입력으로 진행")
    checked = [z for z in cfg.infra.azs if z in azs]
    if azs:
        pick = questionary.checkbox(
            f"  배포할 AZ (최소 2, 현재 {cfg.aws.region})",
            choices=[Choice(z, checked=z in checked) for z in azs]).ask()
        if pick is None:
            return None
        while len(pick) < 2:
            print("  ⚠ DB/ElastiCache subnet group 요건상 AZ 는 2개 이상입니다")
            pick = questionary.checkbox(
                "  배포할 AZ (최소 2)",
                choices=[Choice(z, checked=z in pick) for z in azs]).ask()
            if pick is None:
                return None
        out["infra"]["azs"] = sorted(pick)
    else:
        raw = ask("  AZ 목록 (쉼표 구분, 예: ap-northeast-2a,ap-northeast-2c)",
                  default=",".join(cfg.infra.azs))
        if raw is None:
            return None
        pick = sorted(z.strip() for z in raw.split(",") if z.strip())
        if len(pick) < 2:
            print("  ⚠ AZ 2개 미만 — 기본(리전 처음 두 AZ)으로 진행합니다")
        else:
            out["infra"]["azs"] = pick

    # ── ③ Cognito — hosted-ui domain suffix + user groups
    print("\n■ 3/4 — Cognito (로그인 도메인 + 팀 그룹)")
    acct = cfg.aws.account_id or account_id(cfg.aws.region)
    suffix = ask("  cognito domain suffix "
                 f"(최종 도메인: llm-gateway-{cfg.env}-<suffix>.auth."
                 f"{cfg.aws.region}.amazoncognito.com)",
                 default=cfg.infra.cognito_domain_suffix
                 or default_cognito_suffix(cfg.aws.region, acct))
    if suffix is None:
        return None
    out["infra"]["cognito_domain_suffix"] = suffix.strip()
    print("  사용자 그룹 — 규칙: 'Claude_<team>' 또는 'Claude_<dept>_<team>'.\n"
          "  'Claude_' 접두는 자동 보장, ClaudeAdmin 은 항상 자동 포함.\n"
          "  빈 입력으로 종료.")
    groups: list[str] = list(cfg.infra.cognito_groups)
    while True:
        g = questionary.text("  그룹 추가 (예: AI-Platform_DevOps)",
                             default="").ask()
        if g is None:
            return None
        g = g.strip()
        if not g:
            break
        if g != "ClaudeAdmin" and not g.startswith("Claude_"):
            g = "Claude_" + g
            print(f"    → '{g}' (Claude_ 접두 자동 부여)")
        if g not in groups:
            groups.append(g)
    if groups:
        out["infra"]["cognito_groups"] = groups

    # ── ④ bedrock 허용 모델 + web-search
    print("\n■ 4/4 — Bedrock 허용 모델 + Web Search")
    default_desc = ("모듈 최신 기본" if eks else "models_profile 매핑")
    mode = questionary.select(
        "  bedrock_allowed_model_arns",
        choices=[
            Choice(f"기본값 사용 — {default_desc} "
                   "(Claude 4/5 + GPT-5.6, profile+FM 양쪽. 권장)", "default"),
            Choice("직접 입력 — 쉼표 구분 ARN 패턴 목록", "custom"),
        ], default="default").ask()
    if mode is None:
        return None
    if mode == "custom":
        raw = ask("  ARN 패턴 목록 (쉼표 구분)",
                  default=",".join(cfg.infra.bedrock_allowed_model_arns))
        if raw is None:
            return None
        arns = [a.strip() for a in raw.split(",") if a.strip()]
        if arns:
            out["infra"]["bedrock_allowed_model_arns"] = arns
    ws = questionary.confirm(
        "  server-side Web Search 사용 (AgentCore Gateway — apply 시 자동 "
        "프로비저닝, 실패 시 graceful-off)", default=cfg.features.web_search).ask()
    if ws is None:
        return None
    out["web_search"] = ws
    return out


def persist_bootstrap_answers(config_path: Path, answers: dict) -> None:
    """wizard 답변을 gateway.yaml doc 에 병합 저장 — 다음 실행부터 선언적."""
    doc = yaml.safe_load(config_path.read_text()) or {}
    deploy = doc.setdefault("deploy", {})
    bucket, table = answers["tfstate"]
    deploy["tfstate_bucket"] = bucket
    deploy["tfstate_table"] = table
    infra = {k: v for k, v in (answers.get("infra") or {}).items() if v}
    if infra:
        doc.setdefault("infra", {}).update(infra)
    doc.setdefault("features", {})["web_search"] = bool(
        answers.get("web_search"))
    config_path.write_text(
        "# gateway.yaml — 이 배포의 source of truth.\n"
        "# infra.* 는 deploy bootstrap wizard 가 기록한 신규 설치 입력.\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    config_path.chmod(0o600)
