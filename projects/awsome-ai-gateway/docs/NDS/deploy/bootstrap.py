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
import re
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


def _quiet(argv: list[str], *, input: str | None = None,
           timeout: int = 120) -> subprocess.CompletedProcess:
    """best-effort 조회용 subprocess — 바이너리 부재/행업을 traceback 대신
    통제된 SystemExit 로 (eks_apply._run 과 같은 계약)."""
    try:
        return subprocess.run([str(a) for a in argv], capture_output=True,
                              text=True, input=input, timeout=timeout)
    except FileNotFoundError:
        raise SystemExit(f"{argv[0]} 명령을 못 찾았습니다 — 설치/PATH 확인 필요")
    except subprocess.TimeoutExpired:
        raise SystemExit(f"{argv[0]} 응답 없음({timeout}s) — 네트워크/자격증명 확인 필요")


def _aws(args: list[str], region: str, *, capture: bool = True,
         ok_rc: tuple[int, ...] = (0,)) -> subprocess.CompletedProcess:
    r = _quiet(["aws", *args, "--region", region, "--output", "json"])
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
    검토 주석을 단다 — 잘못된 자동값보다 사람 검토가 낫다. 치환은 전부 앵커
    정규식으로 한다 — 리터럴 replace 는 템플릿 서식이 조금만 달라져도 조용히
    no-op 이 되고, 그러면 신 env 의 tfvars 에 `environment = "dev"` 가 남아
    terraform 이 다른 env 이름의 리소스를 이 state 에 만들어 버린다.
    미매치는 침묵 대신 명시 주석으로 표면한다."""
    misses: list[str] = []

    def _set(t: str, key: str, value: str) -> str:
        nonlocal misses
        new, n = re.subn(rf'(?m)^(\s*{re.escape(key)}\s*=\s*)"[^"]*"',
                         rf'\g<1>"{value}"', t, count=1)
        if not n:
            misses.append(key)
        return new

    t = example
    t = _set(t, "environment", env)
    t = _set(t, "aws_region", region)
    t = _set(t, "cognito_domain_suffix", default_cognito_suffix(region, account))
    t = _set(t, "principal_arn", principal_arn)
    # NDS 배포는 admin-chat-agent(BI 챗)를 포함하지 않는다 — example 은 본
    # deliverable 운영 환경 재현용으로 true 라 신규 설치에선 꺼 둔다.
    # 템플릿 서식이 바뀌어도(공백 수·"true" 인용) 잡히도록 정규식으로.
    t = re.sub(r'(?m)^(\s*enable_chat_agent\s*=\s*)"?true"?',
               r'\g<1>false', t, count=1)
    # example 의 조직 실명 그룹은 복사하지 않는다 — 그대로 apply 하면
    # 남의 조직 그룹이 풀에 생기고 onboard_admin 이 admin 을 첫 그룹에
    # 오배정한다. wizard 답변(infra.cognito_groups)이 managed overlay 로
    # 채우므로 여기선 비워 둔다.
    t = re.sub(
        r'cognito_groups\s*=\s*\[[^\]]*\]',
        'cognito_groups = ["ClaudeAdmin"]  # 팀 그룹은 infra.cognito_groups '
        '(wizard)로 선언 — 여기 추가하면 env apply 에 생성됨',
        t, count=1)
    t = re.sub(r'(?m)^(\s*enable_chat_db_tools\s*=\s*)"?true"?',
               r'\g<1>false', t, count=1)
    header = (
        "# generated by `deploy bootstrap` — terraform.tfvars.example 에서 생성.\n"
        "# ⚠️ 검토 필요: eks_cluster_version/addon_versions(최신 권장으로), "
        "cognito_groups(조직 그룹),\n"
        "#   tags, enable_chat_agent(불필요하면 false). 그 외는 배포 계정 실측값으로 채워짐.\n")
    if misses:
        header += (f"# ⚠️ 템플릿 서식 변경으로 {misses} 치환을 못 했습니다 — "
                   "기본값이 그대로 남아 있으니 수동으로 고치세요.\n")
    return header + "\n" + t


def _sanitize_copied_values(text: str, region: str = "") -> str:
    """dev env values 사본에서 환경 교차 값을 중화한다.

    dev 파일엔 실제 Cognito issuer(`.../ap-northeast-2_xxx`)와 enabled=true 가
    박혀 있다 — 그대로 복사하면 terraform output 이 없거나 OIDC 자동배선이
    스킵되는 배포(domain=none)에서 신 env 가 **dev 풀의 토큰을 신뢰**한다.
    같은 계정에서는 COGNITO_USER_POOL_ID·AGENTCORE_RUNTIME_ARN·role-arn
    어노테이션·imageRegistry·allowedIamRoles 도 dev 리소스를 조용히
    가리킨다 (render/--set 이 덮지 않는 경로 존재) — 전부 공란화해 terraform
    output/render 주입이 유일한 소스가 되게 한다. 주석 보존을 위해 텍스트
    수준으로 고친다."""
    # oidc: 블록의 enabled 만 끈다 — 다른 enabled: 키와 구분하기 위해 대상은
    # oidc 보다 깊은 들여쓰기의 직계 자식. 첫 자식이 enabled: 가 아니어도
    # (issuerUrl 등이 먼저) 같은 들여쓰기의 형제 키를 건너뛰고 찾는다. 손자
    # 키(더 깊은 들여쓰기)는 gap 이 삼켜 매치 대상에서 빠지고 oidc 직계의
    # enabled: 만 잡힌다 — 같은 들여쓰기의 oidc 형제나 탑레벨 라인이 나오면
    # 매치가 끝나 오조작은 없다. gap 의 비주석 대안은 [^\s#] 로 시작을 제한해
    # 깊은 주석 라인이 두 분기 모두에 매치되는 모호성(지수 백트래킹)을 없앤다.
    # \r?\n — CRLF 파일의 빈 줄/주석이 gap 을 끊어 enabled:true 가 살아남는
    # 미스(누출 방향)를 막는다. 탑레벨 oidc:(들여쓰기 0)도 대상이다.
    text = re.sub(
        r"(?m)(^([ \t]*)oidc:[^\n]*\r?\n"
        r"(?:(?:[ \t]*#[^\n]*|[ \t]*)\r?\n)*"
        r"(?:(\2[ \t]+)(?!enabled[ \t]*:)\S[^\n]*\r?\n"
        r"(?:(?:[ \t]*#[^\n]*|[ \t]*)\r?\n|\3[ \t]*[^\s#][^\n]*\r?\n)*"
        r"\3enabled[ \t]*:[ \t]*"
        r"|\2[ \t]+enabled[ \t]*:[ \t]*))"  # group1 은 enabled: 까지 — 값은 그룹 밖
        r"(?i:[\"']?(?:true|yes|on|y)[\"']?)",
        r"\g<1>false", text)
    # flow 스타일 oidc: {enabled: true} — 블록 정규식은 같은 줄 값을 못 본다.
    text = re.sub(
        r"(?i)(\boidc:\s*\{[^}\n]*?\benabled[ \t]*:[ \t]*)"
        r"[\"']?(?:true|yes|on|y)[\"']?",
        r"\g<1>false", text)
    # folded/literal 블록 스칼라(>|)·flow 컬렉션([{) 값 — 스칼라 매치가
    # 헤더 문자만 삼키고 연속 들여쓰기 줄을 남기면 깨진 YAML 이 된다.
    # 구문째 지우되, 들여쓰기가 더 깊은 연속 라인은 값 본문이므로 같이 삼킨다.
    for key in ("issuerUrl", "imageRegistry", "COGNITO_USER_POOL_ID",
                "AGENTCORE_RUNTIME_ARN", "AGENTCORE_GATEWAY_URL",
                "COGNITO_REGION", "AGENTCORE_REGION"):
        text = re.sub(
            rf'(?m)^([ \t]*)({key}:[ \t]*)[>|{{\[][^\n]*\r?\n'
            rf'(?:[ \t]*\r?\n|\1[ \t]+[^\n]*(?:\r?\n|\Z))*',
            r'\g<1>\g<2>""\n', text)
    text = re.sub(r'(?m)^(\s*issuerUrl:\s*)("[^"\n]+"|[^\s#"][^\s#]*)',
                  r'\g<1>""  # scaffold 가 중화 — terraform output 이 apply 시 주입',
                  text)
    # 계정/env 핀값 공란화 — 키는 남기고 값만 지운다 (따옴표 없는 값도 대상).
    # 비어 있는 값("")은 매치하지 않아 재실행해도 멱등이다.
    # bare 값의 첫 글자에서 >|{[ 를 제외 — 블록/flow 헤더를 부분만 삼켜
    # 깨진 YAML 을 만들지 않기 위함 (위 pre-pass 가 구문째 처리한다).
    _val = r'("[^"\n]+"|[^\s#">{}\[\]|][^\s#]*)'
    for key in ("imageRegistry", "COGNITO_USER_POOL_ID",
                "AGENTCORE_RUNTIME_ARN", "AGENTCORE_GATEWAY_URL"):
        text = re.sub(rf'(?m)^(\s*{key}:\s*){_val}', r'\g<1>""', text)
    # 잘못된 풀 동기화로 사용자 비활성화되는 것을 막는다.
    text = re.sub(r'(?m)^(\s*COGNITO_SYNC_DEACTIVATE_MISSING:\s*)'
                  r"(?i:true|yes|\"true\"|'true'|\"yes\"|'yes')",
                  r'\g<1>"false"', text)
    # dev 의 IAM role 이름 허용목록 — 신 env 의 정책과 무관한 값.
    # flow([a, b] — 뒤따르는 주석 포함)와 블록(- item, 중간 주석/빈 줄 허용).
    text = re.sub(r'(?m)^(\s*allowedIamRoles:)[ \t]*\[[^\]]*\][ \t]*(?:#[^\n]*)?$',
                  r'\g<1> []', text)
    text = re.sub(
        r'(?m)^(\s*allowedIamRoles:)[^\n]*\n'
        r'(?:[ \t]*(?:#[^\n]*|-[^\n]*|)\n)+',
        r'\g<1> []\n', text)
    # dev IRSA role ARN — terraform output(gateway_proxy_role_arn 등)이
    # --set 으로 주입하는 값이므로 공란화. 주석 예시(# eks...role-arn)에도
    # 실 계정 ARN 이 박혀 있어 같이 중화한다 (비인용 값 포함).
    text = re.sub(r'(?m)^(\s*#?\s*eks\.amazonaws\.com/role-arn:\s*)'
                  r'("arn:[^"]*"|arn:\S+)', r'\g<1>""', text)
    # 리전 핀값 — render/--set 이 덮지 않는 키(COGNITO_REGION)가 dev 리전을
    # 가리키면 풀을 잘못된 리전에서 찾는다. 배포 리전으로 치환한다.
    # AGENTCORE_REGION 은 adminApi 의 채팅 리전만 대상 — gatewayProxy 의
    # websearch 커넥터는 us-east-1 고정이라 그 값은 건드리지 않는다.
    if region:
        text = re.sub(rf'(?m)^(\s*COGNITO_REGION:\s*){_val}',
                      rf'\g<1>"{region}"', text)
        text = re.sub(
            r'(?m)^(\s*AGENTCORE_REGION:\s*)'
            r'("(?!us-east-1")[^"\n]+"|'
            r'(?!us-east-1\b|[\'"]us-east-1[\'"])[^\s#"][^\s#]*)',
            rf'\g<1>"{region}"', text)
    return text


def scaffold_eks_env(cfg: GatewayConfig, repo_root: Path, region: str) -> Path:
    """deploy.tf_env_dir 또는 기본 경로의 env 디렉토리 — 없으면 템플릿에서 생성."""
    target = repo_root / env_dir(cfg)
    if target.is_dir():
        # tf_env_dir 로 지명된 dir 은 사용자 소유(bring-your-own) — 내용이
        # 비어 있어도 건드리지 않는다 (빈 env 는 env_apply 의 terraform 이
        # 스스로 에러를 낸다). NDS 기본 경로는 scaffold 만 만드므로, 파일
        # 일부만 있는 반쪽 상태(중단)는 variables.tf 기본값 plan 함정이 돼
        # 자동으로 이어서 완성한다.
        if cfg.deploy.tf_env_dir:
            return target
        missing = [f for f in (*TF_FILES, "terraform.tfvars")
                   if not (target / f).exists()]
        if not missing:
            return target
        print(f"⚠ {target} 이 부분 생성 상태입니다(없음: {missing}) — "
              "scaffold 를 이어서 완성합니다", file=sys.stderr)
    template = repo_root / "deployment/terraform/environments" / ENV_TEMPLATE
    if not template.is_dir():
        raise SystemExit(f"env 템플릿 {template} 이 없습니다 — "
                         "deploy.tf_env_dir 로 기존 env 를 지정하십시오")
    target.mkdir(parents=True, exist_ok=True)  # 부분 scaffold 재실행 경로
    for f in TF_FILES:
        text = (template / f).read_text()
        if f == "backend.tf":
            # 템플릿은 dev 의 key/region 이 하드코딩 — 그대로 복사하면 이 env 의
            # state 가 dev/terraform.tfstate 에 묶여 apply 가 dev 인프라를
            # 갈아엎는다. env/region 으로 재작성한다.
            text = re.sub(r'key\s*=\s*"[^"]*"',
                          f'key = "{cfg.env}/terraform.tfstate"',
                          text, count=1)
            text = re.sub(r'region\s*=\s*"[^"]*"',
                          f'region = "{region}"', text, count=1)
        (target / f).write_text(text)
    if not (target / "terraform.tfvars").exists():
        caller = _aws(["sts", "get-caller-identity"], region)
        arn = json.loads(caller.stdout).get("Arn", "")
        # SSO/assume-role 세션 ARN(arn:aws:sts::…:assumed-role/Role/session)은
        # EKS access entry 가 거부한다 — IAM role ARN 으로 정규화.
        m = re.match(
            r"arn:aws:sts::(\d+):assumed-role/([^/]+)/", arn)
        if m:
            arn = f"arn:aws:iam::{m.group(1)}:role/{m.group(2)}"
            print(f"ⓘ assumed-role 세션 ARN 을 IAM role ARN 으로 정규화했습니다: "
                  f"{arn}", file=sys.stderr)
        example = (template / "terraform.tfvars.example").read_text()
        scaffolded = scaffold_tfvars(
            example, env=cfg.env, region=region,
            account=account_id(region), principal_arn=arn)
        # example 에 없는 필수 키를 보강 — 없으면 variables.tf 기본값(EKS 구버전,
        # 다른 리전의 AZ/subnet)이 묵묵히 적용돼 apply 중간 실패가 난다.
        # 클러스터 버전·애드온·AZ 는 템플릿 env 의 실측값을 가져오고, AZ 는
        # wizard 답변(infra.azs)이 있으면 그걸 우선한다.
        live = template / "terraform.tfvars"
        extra: list[str] = []
        if live.exists():
            live_t = live.read_text()
            for key in ("eks_cluster_version", "eks_addon_versions"):
                m = re.search(
                    rf"(?m)^{key}\s*=\s*(\"[^\"]+\"|\{{[^}}]*\}}|\[[^\]]*\])",
                    live_t)
                if m and not re.search(rf"(?m)^\s*{key}\s*=", scaffolded):
                    extra.append(f"{key} = {m.group(1)}")
        azs = cfg.infra.azs
        if not azs:
            try:
                azs = list_azs(region)[:2]
            except SystemExit:
                azs = []
        if azs and not re.search(r"(?m)^\s*azs\s*=", scaffolded):
            extra.append(f"azs = {json.dumps(sorted(azs))}")
        if extra:
            scaffolded += (
                "\n# ── scaffold 자동 보강 (템플릿 실측/wizard 답변 — 검토 후 조정)\n"
                + "\n".join(extra) + "\n")
        (target / "terraform.tfvars").write_text(scaffolded)
    else:
        # 부분 복구 — 기존 tfvars(사람 검토본)는 절대 덮어쓰지 않는다
        print("ⓘ 기존 terraform.tfvars 는 보존하고 tf 파일만 보강했습니다",
              file=sys.stderr)
    print(f"ⓘ terraform env 를 새로 만들었습니다: {target}\n"
          "  terraform.tfvars 의 주석 표시된 항목(cluster 버전·cognito_groups·tags)을 "
          "검토하세요 — 계속 진행하려면 bootstrap 을 다시 실행합니다.",
          file=sys.stderr)

    # env values 파일도 없으면 dev 를 복사 — 없으면 ESO·adminBootstrap·ALB
    # 어노테이션이 차트 기본값이라 helm 이 시크릿 참조 실패로 뜨지 못하고
    # ADMIN_GROUPS 도 비어 관리자가 아무도 못 된다.
    chart_dir = repo_root / "deployment/charts/llm-gateway"
    vals_dst = chart_dir / f"values-eks-fargate-{cfg.env}.yaml"
    # env_values_file 의 탐색 순서와 같은 후보군 — fargate 가 아닌 명명도
    # 우선순위에 들어가므로 어느 하나라도 있으면 사본을 만들지 않는다
    # (만들면 높은 우선순위로 사용자 파일을 가린다).
    existing = [chart_dir / f"values-eks-fargate-{cfg.env}.local.yaml",
                chart_dir / f"values-eks-{cfg.env}.local.yaml",
                vals_dst,
                chart_dir / f"values-eks-{cfg.env}.yaml"]
    if not any(p.exists() for p in existing):
        src = chart_dir / "values-eks-fargate-dev.yaml"
        if src.exists():
            vals_dst.write_text(
                _sanitize_copied_values(src.read_text(), region))
            print(f"ⓘ env values 도 {src.name} 에서 복사했습니다: {vals_dst.name}\n"
                  "  dev 의 OIDC(enabled/issuerUrl)는 중화했습니다 — 신 env 의 "
                  "Cognito 는 terraform output 이 apply 시 주입합니다.\n"
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

def _manual_alb_pins(env_dir_path: Path) -> set[str]:
    """수동 고정된 ALB dimension 변수명 집합 — §8-A 수동 경로 감지용.

    terraform.tfvars + *.auto.tfvars + terraform.tfvars.json + TF_VAR_alb_*
    환경변수를 본다 (전부 terraform 이 실제로 읽는 채널). 주석은 스캐너로
    제거 — `# alb_lb_dimensions` 같은 죽은 선언이 가드를 우회하지 못한다."""
    from .render.common import tfvars_clean
    pinned: set[str] = set()
    for name in ("alb_lb_dimensions", "alb_target_groups"):
        if os.environ.get(f"TF_VAR_{name}") is not None:
            pinned.add(name)
    candidates = [env_dir_path / "terraform.tfvars",
                  env_dir_path / "terraform.tfvars.json",
                  *sorted(env_dir_path.glob("*.auto.tfvars")),
                  *sorted(env_dir_path.glob("*.auto.tfvars.json"))]
    pat = re.compile(r"(?m)^[ \t]*(alb_(?:lb_dimensions|target_groups))[ \t]*=")
    for f in candidates:
        try:
            if not f.exists():
                continue
            text = f.read_text()
        except OSError:
            continue
        if f.suffix == ".json":
            # tfvars.json 은 {"key": value} — 주석 불가라 키 존재면 고정
            pinned.update(n for n in
                          ("alb_lb_dimensions", "alb_target_groups")
                          if re.search(rf'"{n}"\s*:', text))
        else:
            pinned.update(pat.findall(tfvars_clean(text)))
    return pinned


def env_apply(env_dir_path: Path, gen_dir: Path,
              backend: tuple[str, str] | None = None) -> dict:
    """terraform init(+backend-config) → plan(-detailed-exitcode, 출력 스트림)
    → saved plan apply (base tfvars + managed overlay).

    반환: terraform outputs."""
    init = ["terraform", "init", "-input=false"]
    if backend:
        bucket, table = backend
        init += [f"-backend-config=bucket={bucket}",
                 f"-backend-config=dynamodb_table={table}"]
        if (env_dir_path / ".terraform").exists():
            # tfstate 답변이 바뀐 재실행 — 캐시된 backend 로 plan/apply 가
            # 낡은 state 에 붙는 것을 막는다 (state 마이그레이션은 없다).
            init.append("-reconfigure")
    _run(init, cwd=env_dir_path)
    var_args = ["-var-file=terraform.tfvars"]
    managed = gen_dir / "terraform.tfvars"
    if managed.exists():
        # cwd 는 env_dir 라 상대경로는 절대경로로 변환해 넘긴다
        var_args.append(f"-var-file={managed.resolve()}")  # 뒤에 오는 -var-file 이 이긴다
    # 기존 env 재실행 시 발견된 ALB dimension 도 넘긴다 — 빠뜨리면 alb_*=[]
    # 로 평가돼 이미 만든 ALB 알람이 무경고 삭제된다 (bootstrap 은 plan 검증 없음).
    alb_dims = gen_dir / "alerts-dims.tfvars"
    if alb_dims.exists():
        var_args.append(f"-var-file={alb_dims.resolve()}")
        # 파일 dims 와 state dims 가 다르면 stale 가능 — 삭제/생성이
        # auto-approve 된다. 경고만 (신 dims 반영이 정상인 경우도 있음).
        st = eks_apply._run_quiet(["terraform", "state", "list"],
                                cwd=env_dir_path, timeout=120)
        if st.returncode != 0:
            print("ⓘ terraform state list 실패 — 파일/state dimension 대조를 "
                  "건너뜁니다", file=sys.stderr)
        else:
            state_dims = set(re.findall(
                r'aws_cloudwatch_metric_alarm\.alb_target_5xx\["([^"]+)"\]',
                st.stdout))
            state_tgs = set(re.findall(
                r'aws_cloudwatch_metric_alarm\.alb_unhealthy\["([^"]+)"\]',
                st.stdout))
            try:
                _dims_text = alb_dims.read_text()
            except (OSError, UnicodeDecodeError):
                _dims_text = ""
            file_dims = set(re.findall(r'"(app/[^"]+)"', _dims_text))
            file_tgs = set(re.findall(r'"(targetgroup/[^"]+)"', _dims_text))
            # state 에 알람이 하나도 없는 첫 적용은 dims 일치 비교가 무의미
            # — 차이 경고가 매번 오탐된다.
            if state_dims and file_dims != state_dims:
                print(f"⚠ {alb_dims.name} 의 ALB dimension({sorted(file_dims)})"
                      f" 이 state({sorted(state_dims)})와 다릅니다 — stale "
                      "일 수 있으니 `deploy apply` 로 갱신하는 편이 안전합니다",
                      file=sys.stderr)
            if state_tgs and file_tgs != state_tgs:
                print(f"⚠ {alb_dims.name} 의 TargetGroup dimension "
                      f"({sorted(file_tgs)})이 state({sorted(state_tgs)})와 "
                      "다릅니다 — stale 일 수 있으니 `deploy apply` 로 "
                      "갱신하는 편이 안전합니다", file=sys.stderr)
    else:
        # 파일 부재(gen/ 는 gitignore — 신규 머신/클론 기본값)인데 state 에
        # ALB 알람이 있으면 이 apply 가 조용히 전량 삭제한다 — 단 env
        # terraform.tfvars 에 dims 를 수동 고정한 경우(§8-A 수동 경로)는
        # 삭제가 아니므로 차단하지 않는다.
        pinned = _manual_alb_pins(env_dir_path)
        st = eks_apply._run_quiet(["terraform", "state", "list"],
                                cwd=env_dir_path, timeout=120)
        if st.returncode != 0:
            print("ⓘ terraform state list 실패 — ALB 알람 존재 여부를 검증하지 "
                  "못했습니다 (backend 일시 오류면 apply 도 실패합니다)",
                  file=sys.stderr)
        else:
            # 패밀리별 요구 — alb_target_5xx 는 lb_dimensions 가,
            # alb_unhealthy 는 target_groups 가 있어야 삭제가 아닌 유지다.
            # 한쪽만 고정하면 다른 패밀리가 [] 로 평가돼 지워진다.
            need: set[str] = set()
            if 'aws_cloudwatch_metric_alarm.alb_target_5xx[' in st.stdout:
                need.add("alb_lb_dimensions")
            if 'aws_cloudwatch_metric_alarm.alb_unhealthy[' in st.stdout:
                need.add("alb_target_groups")
            missing = need - pinned
            if missing:
                raise SystemExit(
                    f"ALB 알람이 state 에 있지만 {alb_dims} 이 없고 수동 고정"
                    f"({', '.join(sorted(missing))})도 없습니다 — "
                    "이 apply 가 알람을 삭제합니다. 먼저 `deploy apply` 를 "
                    "실행해 ALB dimension 을 발견·기록하거나, 수동 경로라면 "
                    "env terraform.tfvars 에 alb_lb_dimensions/alb_target_groups "
                    "를 고정한 뒤 bootstrap 을 재실행하세요")
    # plan → saved plan → apply: 첫 배포가 수백 리소스를 -auto-approve 로
    # 바로 만들던 계약이었다 — plan 출력이 터미널에 그대로 보이게 하고,
    # 검토된 plan 파일을 그대로 적용해 plan↔apply 사이 drift 도 막는다.
    plan_file = gen_dir / "bootstrap.tfplan"
    try:
        pr = subprocess.run(
            ["terraform", "plan", "-detailed-exitcode", "-input=false",
             "-out", str(plan_file), *var_args],
            cwd=env_dir_path)
    except FileNotFoundError:
        raise SystemExit("terraform 명령을 못 찾았습니다 — 설치와 PATH 확인 필요")
    if pr.returncode == 1:
        raise SystemExit("terraform plan 실패 — 위 오류를 확인하세요")
    if pr.returncode not in (0, 2):
        # -detailed-exitcode 계약 밖의 rc(시그널 kill 등) — 저장된 plan 도
        # 없으므로 적용을 시도하지 않는다
        raise SystemExit(f"terraform plan 비정상 종료(rc={pr.returncode}) — 재시도하세요")
    try:
        if pr.returncode == 0:
            print("ⓘ terraform plan: 변경 없음 — env 인프라가 이미 선언 "
                  "상태입니다 (재실행 안전 경로)", file=sys.stderr)
            return eks_apply.tf_outputs(env_dir_path)
        # rc==2 — 변경 있음. 방금 출력된 그 plan 그대로 적용 (saved plan 은
        # 승인 프롬프트가 없다 — 승인은 위 _confirm_apply 에서 이미 받음)
        _run(["terraform", "apply", "-input=false", str(plan_file)],
             cwd=env_dir_path)
    finally:
        plan_file.unlink(missing_ok=True)  # plan 은 민감값을 담을 수 있어 정리
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
        r = _quiet(["aws", "secretsmanager", "describe-secret",
                    "--secret-id", name, "--region", region,
                    "--output", "json"])
        if r.returncode != 0:
            return False
        # 삭제 예약(pending deletion) 시크릿도 describe 는 된다 — "존재"로
        # 처리하면 create 도 못 하고 ESO sync 도 실패해 helm 이 롤백된다.
        # 진행해봤자 어차피 깨지는 경로라 여기서 멈춘다.
        try:
            if json.loads(r.stdout or "{}").get("DeletedDate"):
                raise SystemExit(
                    f"secrets {name} — 삭제 예약 상태입니다. "
                    "`aws secretsmanager restore-secret --secret-id "
                    f"{name}` 로 복구하거나, 삭제 완료(기본 7일, "
                    "--force-delete-without-recovery 즉시) 후 재실행하세요")
        except json.JSONDecodeError:
            pass
        return True

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
        token = json.loads(pw.stdout or "null")
        if not isinstance(token, str) or not token:
            raise SystemExit(
                f"{arn} 의 SecretString 이 비어/비정상입니다 — elasticache "
                "auth token 시크릿을 확인하세요 (json 오브젝트가 아니라 "
                "문자열 토큰이어야 합니다)")
        _aws(["secretsmanager", "create-secret", "--name", redis,
              "--secret-string", json.dumps({"password": token})], region)
        notes.append(f"secrets {redis} — auth_token 복사 완료")

    db = f"{prefix}{cfg.env}/db"
    # pending-deletion 도 describe 는 된다 — 없는 것과 구분해야 안내가 맞다
    d = _quiet(["aws", "secretsmanager", "describe-secret", "--secret-id", db,
                "--region", region, "--output", "json"])
    if d.returncode == 0:
        try:
            if json.loads(d.stdout or "{}").get("DeletedDate"):
                raise SystemExit(
                    f"secrets {db} — 삭제 예약 상태입니다. restore-secret 으로 "
                    "복구하거나 삭제 완료 후 재실행하세요")
        except json.JSONDecodeError:
            pass
    r = _quiet(
        ["aws", "secretsmanager", "get-secret-value", "--secret-id", db,
         "--query", "SecretString", "--region", region, "--output", "text"])
    if r.returncode != 0:
        raise SystemExit(
            f"secrets {db} 가 없습니다 — terraform env apply 가 만들어야 하는 "
            "시크릿입니다. env apply 결과를 확인하십시오 (절대 수동 create 하지 말 "
            "것 — master_password 를 잃는다)")
    try:
        keys = set(json.loads(r.stdout or "null").keys())
    except (json.JSONDecodeError, AttributeError):
        raise SystemExit(
            f"secrets {db} 의 SecretString 이 JSON 오브젝트가 아닙니다 — "
            "terraform 관리본이어야 합니다 (손대지 말고 env apply 로 복구)")
    if "password" not in keys:
        raise SystemExit(
            f"secrets {db} 에 password 키가 없습니다({sorted(keys)}) — "
            "terraform apply 로 복구하십시오 (put-secret-value 로 손대지 말 것)")
    # master_password 는 더 이상 /db 안에 들어있지 않다 — migration 은 values 의
    # masterPasswordRemoteKey(RDS 관리형 시크릿 rds!cluster-<uuid>)로 직접 읽는다.
    notes.append(f"secrets {db} — terraform 관리본 확인 (password)")
    return notes


# ── 5. 이미지 (§3-5) ─────────────────────────────────────────────────────────

def ensure_ecr_repos(region: str, services: tuple = EKS_SERVICES) -> None:
    for _, repo, _ in services:
        r = _quiet(
            ["aws", "ecr", "create-repository", "--repository-name",
             f"llm-gateway/{repo}",
             "--image-scanning-configuration", "scanOnPush=true",
             "--encryption-configuration", "encryptionType=AES256",
             "--region", region])
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
    r = _quiet(["aws", "ecr", "get-login-password", "--region", region])
    if r.returncode != 0:
        raise SystemExit("ECR 로그인 비밀번호 조회 실패 — 자격증명 확인")
    lr = _quiet(["docker", "login", "--username", "AWS",
                 "--password-stdin", registry], input=r.stdout)
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
    # scheduler 는 admin-api 이미지를 별도 태그로 당길 수 있다 —
    # images.tags.scheduler 핀이 있으면 그 태그도 만들어 둬야 pull 이 성공.
    sched = (cfg.images.tags or {}).get("scheduler")
    api_tag = (cfg.images.tags or {}).get("adminApi") or cfg.images.tag
    if sched and sched != api_tag:
        image = f"{registry}/llm-gateway/admin-api:{sched}"
        _run(["docker", "build", "--platform", "linux/amd64",
              "-t", image, str(repo_root / "admin-api")], cwd=repo_root)
        _run(["docker", "push", image], cwd=repo_root)


def update_kubeconfig(outs: dict, region: str, context: str = "") -> None:
    """신규 EKS 클러스터를 kubeconfig 에 등록한다 (멱등 — update 는 merge).

    첫 배포에서 이게 없으면 helm/kubectl 이 (a) 컨텍스트 없음으로 실패하거나
    (b) 다른 클러스터의 current-context 로 잘못 배포된다. context 지정 시
    --alias 로 그 이름의 컨텍스트를 만들어 eks_apply 의 --kube-context 와
    정합시킨다."""
    cluster = str(outs.get("cluster_name") or "")
    if not cluster:
        print("ⓘ terraform output cluster_name 없음 — kubeconfig 갱신 생략 "
              "(기존 kubeconfig 에 의존합니다)", file=sys.stderr)
        return
    argv = ["aws", "eks", "update-kubeconfig", "--region", region,
            "--name", cluster]
    if context:
        argv += ["--alias", context]
    r = _quiet(argv)
    if r.returncode != 0:
        raise SystemExit(
            f"aws eks update-kubeconfig 실패: {(r.stderr or '').strip()}\n"
            f"  수동 복구: aws eks update-kubeconfig --region {region} "
            f"--name {cluster}" + (f" --alias {context}" if context else ""))
    print(f"ⓘ kubeconfig 갱신 — cluster={cluster}"
          + (f" context={context}" if context else ""), file=sys.stderr)


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
    # gen 레이어가 adminBootstrap.emails = oidc.admin_emails 로 덮어쓴다 —
    # 앱이 실제로 부트스트랩하는 주소와 Cognito 계정이 어긋나면 실관리자는
    # 로그인 불가 + 구주소에 권한 계정이 남는다. 실효(gen) 값을 우선한다.
    if cfg.oidc.admin_emails:
        emails = list(cfg.oidc.admin_emails)
    else:
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
    if email.rsplit("@", 1)[-1].lower() == "example.com":
        notes.append(f"⚠ adminBootstrap.emails[0]={email} — 플레이스홀더 그대로입니다. "
                     "실제 운영자 이메일로 바꾸지 않으면 로그인 불가 사용자가 "
                     "생성됩니다 (ops 문서 §admin-bootstrap 참조)")
    r = _quiet(
        ["aws", "cognito-idp", "admin-get-user", "--user-pool-id", pool,
         "--username", email, "--region", region])
    if r.returncode != 0:
        if "UserNotFoundException" not in (r.stderr or ""):
            # 조회 자체가 실패(throttle/권한)한 것을 없음으로 오인해 create 를
            # 치면 UsernameExists 로 진짜 원인이 가려진다 — 실패를 그대로 표면.
            raise SystemExit(
                f"cognito-idp admin-get-user 실패 — "
                f"{(r.stderr or '').strip()[:200]}\n"
                "일시적 오류일 수 있습니다 — 재시도하세요")
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
        gr = _quiet(
            ["aws", "cognito-idp", "admin-add-user-to-group", "--user-pool-id",
             pool, "--username", email, "--group-name", group,
             "--region", region])
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
    r = _quiet(
        ["aws", "cognito-idp", "list-groups", "--user-pool-id", pool,
         "--region", region, "--output", "json"])
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
    try:
        import questionary
        from questionary import Choice
    except ImportError:
        raise SystemExit(
            "questionary 가 없습니다 — `pip install -r "
            "docs/NDS/deploy/requirements.txt` 또는 docs/NDS/.venv 생성 후 "
            "사용하십시오.")

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
            # 아무것도 기록하지 않는다 — 실제로 쓰이는 값은 env terraform.tfvars
            # 나 variables.tf 의 기본 azs(ap-northeast-2a/c 핀)라 "리전 처음 두
            # AZ" 가 아니다. 다른 리전 배포면 scaffold 후 직접 고쳐야 한다.
            print("  ⚠ AZ 2개 미만 — azs 를 기록하지 않습니다. env tfvars/"
                  "variables.tf 기본값(ap-northeast-2a/c)이 그대로 쓰입니다 — "
                  f"{cfg.aws.region} 가 아니라면 scaffold 후 azs 를 수정하세요")
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


def persist_bootstrap_answers(config_path: Path, answers: dict) -> bool:
    """wizard 답변을 gateway.yaml doc 에 병합 저장 — 다음 실행부터 선언적.

    반환은 저장 여부 — 스키마 검증에서 스킵되면 False. 호출자는 False 면
    wizard 의 커스텀 값(특히 tfstate 이름)을 in-memory 로 쓰지 말아야 한다:
    yaml 에 없는 이름으로 state 를 만들면 다음 apply 가 선언된 기본 backend
    로 reconfigure 해 split-brain 이 된다."""
    doc = yaml.safe_load(config_path.read_text()) or {}
    if not isinstance(doc, dict):
        doc = {}
    deploy = doc.setdefault("deploy", {})
    if not isinstance(deploy, dict):
        # deploy: null 같은 명시 null — 스키마는 허용(from_dict 가 {} 로
        # 정규화)하지만 setdefault 는 None 을 그대로 돌려줘 크래시가 난다
        deploy = doc["deploy"] = {}
    bucket, table = answers["tfstate"]
    deploy["tfstate_bucket"] = bucket
    deploy["tfstate_table"] = table
    infra = {k: v for k, v in (answers.get("infra") or {}).items() if v}
    if infra:
        if not isinstance(doc.get("infra"), dict):
            doc["infra"] = {}  # infra: null 도 스키마 유효 — 갱신 전 정규화
        doc["infra"].update(infra)
    # tfstate 재시도처럼 web_search 답변이 없는 호출에서 None → false 로
    # 덮어써 기존 선언을 지우는 사고 방지 — 키가 있을 때만 쓴다.
    if "web_search" in answers:
        if not isinstance(doc.get("features"), dict):
            doc["features"] = {}  # features: null 정규화 (infra 와 동일)
        doc["features"]["web_search"] = bool(answers.get("web_search"))
    # 쓰기 전 검증 — 기존 파일이 유효한데 wizard 답변이 스키마를 깨면 파일이
    # 그대로 죽어 이후 모든 deploy 명령이 _load_config 에서 멈춘다. 기존 파일이
    # 이미 무효했다면 답변 탓이 아니므로 저장한다 (load 는 어차피 실패하지만
    # 답변이 파일에 남아 수정이 쉽다).
    from . import schema as _schema
    try:
        _schema.from_dict(doc)
    except _schema.SchemaError as exc:
        try:
            _schema.from_dict(yaml.safe_load(
                config_path.read_text()) or {})
        except _schema.SchemaError:
            pass  # 원본도 이미 무효 — 답변 탓이 아니니 저장 진행
        else:
            print(f"⚠ wizard 답변이 스키마 검증을 통과하지 못해 gateway.yaml 에 "
                  f"저장하지 않습니다 (배포는 기본값으로 계속 진행):\n{exc}",
                  file=sys.stderr)
            return False
    tmp = config_path.with_name(config_path.name + ".tmp")
    tmp.write_text(
        "# gateway.yaml — 이 배포의 source of truth.\n"
        "# infra.* 는 deploy bootstrap wizard 가 기록한 신규 설치 입력.\n"
        + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True))
    tmp.replace(config_path)  # 원자적 교체 — 중간 크래시가 yaml 을 반만 남기지 않게
    config_path.chmod(0o600)
    return True
