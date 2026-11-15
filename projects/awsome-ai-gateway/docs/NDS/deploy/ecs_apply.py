# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""ecs backend apply — terraform → ECR push → terraform(migration+services).

순서가 중요하다:
  1. terraform apply -target=ECR repos  — push 할 repo 를 먼저 만든다
  2. image build + push                 — task def 가 참조할 태그를 올린다
  3. terraform apply (전체)             — task def → migration task(local-exec) →
                                        services. 서비스는 migration 완료를 기다린다
                                        (terraform_data + depends_on — helm hook 과 같은 계약)
  4. services-stable wait               — circuit breaker 가 실패 배포를 롤백

각 단계는 멱등하다 — 중간에 실패해도 다시 apply 하면 이어간다. 단, push 된
이미지가 없으면 migration task 가 pull 실패하므로 1→2→3 순서를 지킨다.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from .schema import GatewayConfig

ENV_DIR = Path("deployment/terraform/environments/gateway-ecs")

# ECR repo 이름 ↔ docker build context (repo root 기준)
BUILD_CONTEXTS = {
    "gateway-proxy": "gateway-proxy",
    "admin-api": "admin-api",
    "admin-ui": "admin-ui",
    "cost-recorder-worker": "cost-recorder-worker",
    "notification-worker": "notification-worker",
    "migration": "db",
}


def _run(argv: list[str], *, cwd: Path | None = None, capture: bool = False) -> subprocess.CompletedProcess:
    print(f"$ {' '.join(str(a) for a in argv)}", file=sys.stderr)
    try:
        r = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=capture, text=True)
    except FileNotFoundError:
        raise SystemExit(f"{argv[0]} 명령을 못 찾았습니다 — 설치와 PATH 확인 필요")
    if r.returncode != 0:
        if capture:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
        raise SystemExit(f"명령 실패 ({r.returncode}): {' '.join(str(a) for a in argv)}")
    return r


def tf_outputs(env_dir: Path) -> dict:
    r = _run(["terraform", "output", "-json"], cwd=env_dir, capture=True)
    try:
        raw = json.loads(r.stdout or "{}")
    except json.JSONDecodeError:
        return {}
    return {k: v.get("value") for k, v in raw.items()}


def terraform_plan(cfg: GatewayConfig, gen_dir: Path, repo_root: Path) -> None:
    """적용하지 않고 변경 계획만 출력 — apply 전 확인/--plan 용."""
    if cfg.features.bi_insight:
        from . import chatagent
        chatagent.ensure_lambda_build(repo_root)
    env_dir = repo_root / ENV_DIR
    # -reconfigure: 모든 ecs env 가 같은 env 디렉토리를 공유하고 env 마다
    # backend key 가 다르다 — 다른 env 의 캐시가 있으면 reconfigure 없는 init 은
    # "Backend configuration changed" 로 멈추고, 멈추지 않으면 낡은 state 에
    # 붙는다 (doctor/teardown 경로와 같은 보호)
    _run(["terraform", "init", "-input=false", "-reconfigure",
          f"-backend-config={gen_dir / 'backend.hcl'}"], cwd=env_dir)
    _run(["terraform", "plan", "-input=false",
          f"-var-file={gen_dir / 'terraform.tfvars'}"], cwd=env_dir)


def terraform_apply(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, *, bootstrap: bool) -> dict:
    env_dir = repo_root / ENV_DIR
    var_file = gen_dir / "terraform.tfvars"
    backend = gen_dir / "backend.hcl"

    _run(["terraform", "init", "-input=false", "-reconfigure",
          f"-backend-config={backend}"], cwd=env_dir)
    if bootstrap:
        # ECR repo 만 먼저 — 이미지 push 대상이 있어야 task def/migration 이 pull 성공한다
        _run(["terraform", "apply", "-auto-approve", "-input=false",
              f"-var-file={var_file}", "-target=module.gateway.aws_ecr_repository.svc"],
             cwd=env_dir)
        return {}
    _run(["terraform", "apply", "-auto-approve", "-input=false", f"-var-file={var_file}"],
         cwd=env_dir)
    return tf_outputs(env_dir)


def push_images(cfg: GatewayConfig, outputs: dict, repo_root: Path) -> None:
    repos = outputs.get("ecr_repository_urls") or {}
    if not repos:
        if cfg.images.registry:
            print("ⓘ 외부 image_registry 사용 — push 는 사용자가 수행해야 합니다", file=sys.stderr)
            return
        print("ⓘ ECR repo 출력이 없습니다 — create_ecr_repos=false?", file=sys.stderr)
        return

    registry_host = next(iter(repos.values())).split("/")[0]
    # shell 파이프 대신 두 프로세스 — registry_host 는 terraform output 이라
    # 셸 문자열에 넣으면 인젝션 표면이 된다 (비밀번호는 stdin 으로만)
    try:
        pw = subprocess.run(["aws", "ecr", "get-login-password",
                             "--region", cfg.aws.region],
                            capture_output=True, text=True, timeout=30)
        if pw.returncode != 0:
            raise SystemExit(f"ECR 로그인 실패: {pw.stderr.strip()[:200]}")
        login = subprocess.run(["docker", "login", "--username", "AWS",
                                "--password-stdin", registry_host],
                               input=pw.stdout, capture_output=True,
                               text=True, timeout=30)
        if login.returncode != 0:
            raise SystemExit(f"ECR 로그인 실패: {login.stderr.strip()[:200]}")
    except subprocess.TimeoutExpired as exc:
        raise SystemExit(f"ECR 로그인 타임아웃: {exc}")
    except FileNotFoundError as exc:
        raise SystemExit(f"{exc.filename} 명령을 못 찾았습니다 — aws/docker 설치 필요")

    for name, ctx in BUILD_CONTEXTS.items():
        url = repos.get(name)
        if not url:
            # 부분 출력 = task-def 가 참조할 이미지가 없음 — push 후 배포가
            # pull 실패로 죽기 전에 여기서 멈춘다
            raise SystemExit(f"ecr_repository_urls 에 '{name}' repo 가 없습니다 — "
                             "terraform output 확인 필요")
        image = f"{url}:{cfg.images.tag}"
        # Fargate 는 x86_64 기본 — ARM 호스트(Apple Silicon/ARM CI)에서 빌드한
        # arm64 이미지는 push 는 되지만 exec-format 으로 죽는다
        _run(["docker", "build", "--platform", "linux/amd64",
              "-t", image, str(repo_root / ctx)])
        # IMMUTABLE repo — 같은 태그 재push 는 ImageTagAlreadyExists 로 죽는다.
        # 재시도/설정만 바꾼 apply 가 매번 여기서 멈추지 않게 이미 있으면 건너뛴다.
        chk = subprocess.run(
            ["aws", "ecr", "describe-images", "--repository-name",
             url.split("/", 1)[-1], "--image-ids", f"imageTag={cfg.images.tag}",
             "--region", cfg.aws.region, "--output", "json"],
            capture_output=True, text=True, timeout=30)
        if chk.returncode == 0:
            print(f"ⓘ {image} — 이미 존재 (immutable repo) — push 건너뜀. "
                  "내용이 바뀌었으면 images.tag 를 올리세요", file=sys.stderr)
            continue
        _run(["docker", "push", image])


def wait_services(cfg: GatewayConfig, outputs: dict) -> None:
    names = list((outputs.get("service_names") or {}).values())
    if not names:
        return
    cluster = outputs.get("ecs_cluster_name")
    if not cluster:
        print("⚠ ecs_cluster_name output 없음 — 안정화 대기를 건너뜁니다",
              file=sys.stderr)
        return
    print("서비스 안정화 대기 중...", file=sys.stderr)
    r = subprocess.run(["aws", "ecs", "wait", "services-stable", "--cluster",
                        cluster, "--services", *names,
                        "--region", cfg.aws.region],
                       capture_output=True, text=True, timeout=900)
    if r.returncode != 0:
        # 타임아웃이 곧 실패는 아니다 — 느린 롤아웃이 뒤에서 완료될 수 있어
        # 아래 describe 로 실제 상태를 확인하고 판정한다
        print("⚠ services-stable 대기 시간 초과 — 배포 상태를 직접 확인합니다",
              file=sys.stderr)
    # circuit-breaker 롤백도 'stable' 로 보고된다 — primary 배포의
    # rolloutState 를 봐야 실패 배포를 성공으로 오인하지 않는다
    d = subprocess.run(["aws", "ecs", "describe-services", "--cluster", cluster,
                        "--services", *names, "--region", cfg.aws.region,
                        "--output", "json"], capture_output=True, text=True,
                       timeout=60)
    if d.returncode != 0:
        print(f"⚠ describe-services 실패 — 배포 상태 미확인: "
              f"{d.stderr.strip()[:150]}", file=sys.stderr)
        return
    try:
        svcs = json.loads(d.stdout).get("services", [])
    except json.JSONDecodeError:
        svcs = []
    failed = [s.get("serviceName") for s in svcs
              if any(dep.get("rolloutState") == "FAILED"
                     for dep in (s.get("deployments") or []))]
    if failed:
        raise SystemExit(
            f"배포 실패(circuit-breaker 롤백) — {', '.join(failed)}. "
            "task 로그/이미지 태그를 확인하고 다시 apply 하세요")
    if r.returncode != 0:
        print("ⓘ 배포가 아직 진행 중일 수 있습니다 — "
              "`deploy doctor` 로 완료 여부를 확인하세요", file=sys.stderr)


# ------------------------------------------------------------------------------
# 동적 후처리 값 — apply 가 알아내는 주소들. extra-vars.json 에 기록돼 다음
# render 에 흡수된다 (gateway.yaml 은 건드리지 않는다 — 발견값이지 선언값이 아님)
# ------------------------------------------------------------------------------
def _extra_path(gen_dir: Path) -> Path:
    return gen_dir / "extra-vars.json"


def load_extra_vars(gen_dir: Path) -> dict:
    p = _extra_path(gen_dir)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_extra_vars(gen_dir: Path, extra: dict) -> None:
    _extra_path(gen_dir).write_text(json.dumps(extra, indent=2))


def _derived_url_vars(cfg: GatewayConfig, outputs: dict) -> dict:
    """도메인 없는 모드이면 ALB DNS 기반 외부 URL — task-def NEXTAUTH_URL 등에 필요.
    tfvars 렌더와 같은 조건을 써야 한다 — cloudfront-temp 는 ecs 경로에서
    domain_name="" 으로 수렴하는데 여기서 none 만 보면 NEXTAUTH_URL 이
    영원히 비어 admin-ui OIDC 콜백이 깨진다."""
    if cfg.domain.mode not in ("none", "cloudfront-temp") or not outputs.get("alb_dns_name"):
        return {}
    dns = outputs["alb_dns_name"]
    return {
        "admin_ui_nextauth_url": f"http://{dns}:3000",
        "gateway_external_url": f"http://{dns}:8000",
        "admin_api_external_url": f"http://{dns}:8080",
    }


def apply(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, *, skip_push: bool = False) -> dict:
    """전체 apply 파이프라인. 반환: terraform outputs (배포된 엔드포인트 포함).

    domain=none 의 2-phase: 1차 apply 로 ALB DNS 를 얻고, 얻은 주소를
    extra-vars.json + tfvars 에 기록한 뒤 2차 apply — admin-ui 의
    NEXTAUTH_URL 이 실제 접속 주소를 가리키게 한다 (helm 의
    install-eks.sh --set adminUi.nextauthUrl=http://<alb-dns> 와 같은 결)."""
    from .render import ecs as ecs_render
    from . import websearch

    if cfg.features.bi_insight:
        # archive_file data source 가 build/ 트리를 zip 한다 — 없으면 tf
        # plan/apply 가 'missing directory' 로 실패. 어떤 tf 명령보다 먼저.
        from . import chatagent
        chatagent.ensure_lambda_build(repo_root)

    # 이전 apply 가 발견한 동적 값이 있으면 tfvars 에 반영한다
    existing = load_extra_vars(gen_dir)

    # install-guide §5-1/§5-2 자동화 — AgentCore Gateway URL 은 프로비저닝
    # 결과물이라 선언이 아니라 apply 산출물이다. extra-vars.json 에 기록해
    # 다음 apply 도 같은 URL 로 렌더한다 (프로비저너는 멱등이라 재사용).
    if cfg.features.web_search and not existing.get("agentcore_gateway_url"):
        url = websearch.ensure_gateway_url(cfg.env, repo_root)
        if url:
            existing["agentcore_gateway_url"] = url
            _save_extra_vars(gen_dir, existing)
        else:
            print("⚠ web_search URL 을 얻지 못했습니다 — WEB_SEARCH_ENABLED 없이 "
                  "진행합니다 (라우터가 검색을 건너뜀)", file=sys.stderr)
    elif not cfg.features.web_search and existing.pop("agentcore_gateway_url", None):
        # 선언을 껐으면 잔여 URL 도 지운다 — taskdef 의
        # agentcore_gateway_url != "" → WEB_SEARCH_ENABLED=true 조건을 되돌림
        _save_extra_vars(gen_dir, existing)

    # 도메인 모드를 none/cloudfront-temp → route53-acm 으로 올리면 이전에 기록한
    # ALB-DNS 파생 URL 이 남아 모듈의 도메인 유도 URL 을 덮어쓴다 — 로그인
    # 콜백 불일치로 admin 로그인이 깨지므로 모드 전환 시 stale 키를 지운다
    if cfg.domain.mode not in ("none", "cloudfront-temp"):
        for k in ("admin_ui_nextauth_url", "gateway_external_url",
                  "admin_api_external_url"):
            existing.pop(k, None)

    bi_popped = False
    if not cfg.features.bi_insight:
        bi_popped = bool(existing.pop("agentcore_runtime_arn", None))
        if bi_popped:
            # 선언을 껐으면 잔여 ARN 도 지운다 — agentcore_runtime_arn != ""
            # 조건이 taskdef 에 AGENTCORE_RUNTIME_ARN env 를 남겨 라우터가
            # 삭제된 Runtime 을 계속 호출하는 것을 되돌림 (web_search 패턴).
            _save_extra_vars(gen_dir, existing)

    # ops/NDS-01 자동화 — 함수 자체는 terraform 모듈 밖 리소스라 boto3 로 보장.
    # task-role invoke 권한과 LITELLM_PRICING_LAMBDA env 는
    # litellm_pricing_lambda tfvars 가 모듈에서 연다 (attach_invoke=False).
    # 함수를 못 만들었는데 이름만 주입하면 admin-api 의 단가 조회가 런타임
    # 에러를 내고 '미주입 = 503 graceful-off' 계약을 깬다 — ensure 성공분만
    # extra-vars 에 실어 tfvars 로 내보낸다.
    if cfg.features.pricing_lambda:
        from . import pricinglambda
        if pricinglambda.ensure(cfg, repo_root, attach_invoke=False):
            existing["litellm_pricing_lambda"] = pricinglambda.fn_name(cfg.env)
        else:
            existing.pop("litellm_pricing_lambda", None)
            print("⚠ pricing Lambda 보장 실패 — LITELLM_PRICING_LAMBDA 없이 "
                  "진행합니다 (단가 조회는 503 graceful-off)", file=sys.stderr)
    else:
        existing.pop("litellm_pricing_lambda", None)
    # extra-vars 는 apply 간 영속 저장소 — 키를 메모리에만 두면 저장본과
    # tfvars 가 어긋나 다음 run 이 var 삭제로 drift 를 낸다
    _save_extra_vars(gen_dir, existing)

    if cfg.features.alerting.enabled:
        # ecs 경로는 ALB 차원 자동 발견이 없다 — enable_alerting 은 SNS+Aurora/
        # ElastiCache 알람만 만들고 ALB 5xx/비정상타깃 알람은 생기지 않는다
        print("ⓘ ecs alerting: ALB 5xx/타겟헬스 알람은 미지원입니다 — "
              "SNS·Aurora·ElastiCache 알람만 생성됩니다", file=sys.stderr)

    ecs_render.render(cfg, gen_dir, extra_vars=existing)

    # teardown 후 7일 복구유예 중인 동명 시크릿은 create 에서 'scheduled for
    # deletion' 으로 죽는다 — plan 이 중간에 실패하기 전에 안내한다
    for sname in (f"/llm-gateway/{cfg.env}/app", f"/llm-gateway/{cfg.env}/db"):
        chk = subprocess.run(
            ["aws", "secretsmanager", "describe-secret", "--secret-id", sname,
             "--region", cfg.aws.region, "--output", "json"],
            capture_output=True, text=True, timeout=30)
        if chk.returncode == 0:
            try:
                meta = json.loads(chk.stdout)
            except json.JSONDecodeError:
                meta = {}
            if meta.get("DeletedDate"):
                raise SystemExit(
                    f"{sname} 가 삭제 유예(7일) 상태입니다 — 재설치 전 "
                    f"`aws secretsmanager restore-secret --secret-id {sname}` "
                    "로 복원하거나 유예 종료를 기다리세요")

    terraform_apply(cfg, gen_dir, repo_root, bootstrap=True)
    partial = tf_outputs(repo_root / ENV_DIR)

    if not cfg.features.bi_insight:
        # 켰던 흔적(stale ARN 또는 아직 살아있는 모듈 outputs)이 있을 때만
        # 프로비저너 소유 Runtime 을 멱등 정리 — 한 번도 안 켰으면 agentcore
        # API 호출/권한 경고를 내지 않게 건너뛴다. 반드시 위 terraform_apply
        # 의 init 이후에 읽는다 — init 전 stale .terraform 캐시가 다른 env 의
        # chat_agent_name 을 돌려줘 남의 Runtime 을 지울 수 있다.
        module_alive = bool(partial.get("chat_agent_name"))
        if bi_popped or module_alive:
            from . import chatagent
            chatagent.teardown(
                repo_root, cfg.env, region=cfg.aws.region,
                # project 변수를 바꾼 env 에선 도출 이름과 실측이 어긋나
                # Runtime 이 새어나간다 — outputs 실측을 우선한다.
                chat_agent_name=partial.get("chat_agent_name", ""))
    if not skip_push:
        push_images(cfg, partial, repo_root)
    outputs = terraform_apply(cfg, gen_dir, repo_root, bootstrap=False)

    # bi_insight — 인프라(module.agentcore_runtime + chat-reader-pw task def)는
    # 위 terraform_apply 가 만들었으므로 여기서 Runtime 프로비저닝 + chat_reader
    # 비밀번호 동기화(ecs run-task)를 실행하고 ARN 을 extra-vars 로 기록한다.
    # 실패해도 배포를 중단하지 않는다 — env 미주입으로 라우터가 503 graceful-off.
    runtime_arn = ""
    if cfg.features.bi_insight:
        from . import chatagent
        runtime_arn = chatagent.ensure_runtime_arn(repo_root, outputs, extras={
            "ECS_CLUSTER": outputs.get("ecs_cluster_name"),
            "ECS_PW_TASKDEF_ARN": outputs.get("chat_reader_pw_task_definition_arn"),
            "ECS_PW_LOG_GROUP": outputs.get("chat_reader_pw_log_group_name"),
            "ECS_SUBNETS": ",".join(outputs.get("private_subnet_ids") or []),
            "ECS_SG": outputs.get("task_security_group_id"),
            "ECS_REGION": cfg.aws.region,
        }, env_name=cfg.env, region=cfg.aws.region)
        if not runtime_arn:
            print("⚠ bi_insight: Runtime ARN 을 얻지 못했습니다 — /chat 은 "
                  "503 (미구성) 상태로 진행합니다", file=sys.stderr)

    # 동적 값(Runtime ARN·외부 URL)이 새로 발견되면 기록하고 task-def 반영을
    # 위해 한 번 더 apply — Runtime ARN 은 admin-api env 주입이라 필수 재적용.
    existing = load_extra_vars(gen_dir)
    discovered = _derived_url_vars(cfg, outputs)
    if runtime_arn:
        discovered["agentcore_runtime_arn"] = runtime_arn
    if discovered and any(existing.get(k) != v for k, v in discovered.items()):
        existing.update(discovered)
        _save_extra_vars(gen_dir, existing)
        ecs_render.render(cfg, gen_dir, extra_vars=existing)
        print("동적 값 발견 — task-def env 반영을 위해 재적용...", file=sys.stderr)
        outputs = terraform_apply(cfg, gen_dir, repo_root, bootstrap=False)

    wait_services(cfg, outputs)
    return outputs
