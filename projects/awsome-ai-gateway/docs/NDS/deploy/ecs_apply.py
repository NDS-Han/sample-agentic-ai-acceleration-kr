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
    r = subprocess.run([str(a) for a in argv], cwd=cwd, capture_output=capture, text=True)
    if r.returncode != 0:
        if capture:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
        raise SystemExit(f"명령 실패 ({r.returncode}): {' '.join(str(a) for a in argv)}")
    return r


def tf_outputs(env_dir: Path) -> dict:
    r = _run(["terraform", "output", "-json"], cwd=env_dir, capture=True)
    raw = json.loads(r.stdout or "{}")
    return {k: v.get("value") for k, v in raw.items()}


def terraform_apply(cfg: GatewayConfig, gen_dir: Path, repo_root: Path, *, bootstrap: bool) -> dict:
    env_dir = repo_root / ENV_DIR
    var_file = gen_dir / "terraform.tfvars"
    backend = gen_dir / "backend.hcl"

    _run(["terraform", "init", "-input=false", f"-backend-config={backend}"], cwd=env_dir)
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
    login = subprocess.run(
        f"aws ecr get-login-password --region {cfg.aws.region} | "
        f"docker login --username AWS --password-stdin {registry_host}",
        shell=True, capture_output=True, text=True)
    if login.returncode != 0:
        raise SystemExit(f"ECR 로그인 실패: {login.stderr}")

    for name, ctx in BUILD_CONTEXTS.items():
        url = repos.get(name)
        if not url:
            continue
        image = f"{url}:{cfg.images.tag}"
        _run(["docker", "build", "-t", image, str(repo_root / ctx)])
        _run(["docker", "push", image])


def wait_services(cfg: GatewayConfig, outputs: dict) -> None:
    names = list((outputs.get("service_names") or {}).values())
    if not names:
        return
    print("서비스 안정화 대기 중...", file=sys.stderr)
    _run(["aws", "ecs", "wait", "services-stable", "--cluster", outputs["ecs_cluster_name"],
          "--services", *names, "--region", cfg.aws.region])


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
    """domain=none 이면 ALB DNS 기반 외부 URL — task-def NEXTAUTH_URL 등에 필요."""
    if cfg.domain.mode != "none" or not outputs.get("alb_dns_name"):
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

    # 이전 apply 가 발견한 동적 값이 있으면 tfvars 에 반영한다
    ecs_render.render(cfg, gen_dir, extra_vars=load_extra_vars(gen_dir))

    terraform_apply(cfg, gen_dir, repo_root, bootstrap=True)
    partial = tf_outputs(repo_root / ENV_DIR)
    if not skip_push:
        push_images(cfg, partial, repo_root)
    outputs = terraform_apply(cfg, gen_dir, repo_root, bootstrap=False)

    # 동적 주소가 새로 발견되면 기록하고 task-def 반영을 위해 한 번 더 apply
    existing = load_extra_vars(gen_dir)
    discovered = _derived_url_vars(cfg, outputs)
    if discovered and any(existing.get(k) != v for k, v in discovered.items()):
        existing.update(discovered)
        _save_extra_vars(gen_dir, existing)
        ecs_render.render(cfg, gen_dir, extra_vars=existing)
        print("외부 URL 발견 — NEXTAUTH_URL 등 반영을 위해 재적용...", file=sys.stderr)
        outputs = terraform_apply(cfg, gen_dir, repo_root, bootstrap=False)

    wait_services(cfg, outputs)
    return outputs
