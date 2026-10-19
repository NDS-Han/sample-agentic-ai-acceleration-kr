# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""bootstrap — 신규 설치 오케스트레이션 단위 테스트."""
import json
import subprocess

import pytest

from deploy import bootstrap


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _cfg(**over):
    from deploy import schema
    doc = {
        "version": 1, "env": "dev",
        "aws": {"region": "ap-northeast-2", "account_id": "123456789012"},
        "deploy": {"target": "eks", "size_tier": "t3"},
        "features": {"notifications": {"provider": "mock"}},
        "images": {"tag": "v1"},
    }
    doc.update(over)
    return schema.from_dict(doc)


# ── env scaffold ──────────────────────────────────────────────────────────────

def test_scaffold_tfvars_substitution():
    example = ('environment = "dev"\n'
               'aws_region  = "ap-northeast-2"\n'
               'cognito_domain_suffix = "vanilla-auth-CHANGE_ACCOUNT_ID"\n'
               'principal_arn = "arn:aws:iam::ACCOUNT_ID:role/YOUR_ROLE"\n')
    out = bootstrap.scaffold_tfvars(
        example, env="stg", region="us-west-2", account="123456789012",
        principal_arn="arn:aws:iam::123456789012:role/deployer")
    assert 'environment = "stg"' in out
    assert 'aws_region  = "us-west-2"' in out
    assert 'stg-auth-123456789012' in out
    assert "role/deployer" in out
    assert "검토 필요" in out


def test_scaffold_eks_env_existing_dir_is_noop(tmp_path):
    cfg = _cfg(deploy={"target": "eks", "size_tier": "t3",
                       "tf_env_dir": "existing/env"})
    (tmp_path / "existing/env").mkdir(parents=True)
    assert bootstrap.scaffold_eks_env(cfg, tmp_path, "r") == tmp_path / "existing/env"


# ── tfstate ───────────────────────────────────────────────────────────────────

def test_ensure_tfstate_eks_naming(monkeypatch, tmp_path):
    seen = {}
    monkeypatch.setattr(bootstrap, "_run",
                        lambda argv, **k: seen.update(env=k.get("env", {})))
    bucket, table = bootstrap.ensure_tfstate(_cfg(), tmp_path, eks=True)
    assert bucket == "llm-gateway-vanilla-tfstate-123456789012"
    assert table == "llm-gateway-vanilla-tflock"
    assert seen["env"]["TFSTATE_BUCKET"] == bucket


def test_ensure_tfstate_ecs_naming_differs(monkeypatch, tmp_path):
    """ecs backend.hcl 추론명(llm-gateway-tfstate-)과 eks env 관례
    (llm-gateway-vanilla-tfstate-)는 다르다 — 섞이면 init 이 엉뚱한 버킷을 본다."""
    monkeypatch.setattr(bootstrap, "_run", lambda argv, **k: None)
    bucket, _ = bootstrap.ensure_tfstate(_cfg(), tmp_path, eks=False)
    assert bucket == "llm-gateway-tfstate-123456789012"


# ── 시크릿 시드 (§3-4) ────────────────────────────────────────────────────────

def _aws_router(secrets_map):
    """describe/create/get-secret-value 를 흉내내는 aws 라우터."""
    calls = []
    def fake(cmd, capture_output=True, text=True, **kw):
        calls.append(cmd)
        name = cmd[cmd.index("--secret-id") + 1] if "--secret-id" in cmd else (
            cmd[cmd.index("--name") + 1] if "--name" in cmd else "")
        if "describe-secret" in cmd:
            return _cp(0 if name in secrets_map else 1)
        if "get-secret-value" in cmd:
            return _cp(0, json.dumps(secrets_map[name]) if "--query" not in cmd
                       else json.dumps(secrets_map[name]))
        if "create-secret" in cmd:
            secrets_map[name] = json.loads(cmd[cmd.index("--secret-string") + 1])
            return _cp(0)
        return _cp(0)
    return fake, calls


def test_seed_secrets_creates_app_and_redis_copies_db_verified(
        monkeypatch, tmp_path):
    cfg = _cfg()
    monkeypatch.setattr(bootstrap, "secret_prefix", lambda c, r: "/llm-gateway/")
    secrets_map = {
        "/llm-gateway/dev/db": {"password": "p", "master_password": "m"},
    }
    fake, _ = _aws_router(secrets_map)   # describe-secret + db get-secret-value
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)

    def fake_aws(args, region, **k):      # create-secret + auth-token 조회
        if "create-secret" in args:
            name = args[args.index("--name") + 1]
            secrets_map[name] = json.loads(
                args[args.index("--secret-string") + 1])
        return _cp(0, json.dumps("redis-auth-token"))
    monkeypatch.setattr(bootstrap, "_aws", fake_aws)
    notes = bootstrap.seed_secrets(
        cfg, tmp_path, {"elasticache_auth_token_secret_arn": "arn:token"})
    app = secrets_map["/llm-gateway/dev/app"]
    # 차트 secret.yaml remoteRef 가 요구하는 4키 전부
    assert set(app) == {"virtual_key_encryption_key", "nextauth_secret",
                        "jwt_jwks_cache_key", "internal_api_token"}
    assert secrets_map["/llm-gateway/dev/redis"] == {"password": "redis-auth-token"}
    assert any("master_password" in n for n in notes)


def test_seed_secrets_preserves_existing(monkeypatch, tmp_path):
    cfg = _cfg()
    monkeypatch.setattr(bootstrap, "secret_prefix", lambda c, r: "/llm-gateway/")
    secrets_map = {
        "/llm-gateway/dev/app": {"x": "keep"},
        "/llm-gateway/dev/redis": {"password": "keep"},
        "/llm-gateway/dev/db": {"password": "p", "master_password": "m"},
    }
    fake, calls = _aws_router(secrets_map)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    bootstrap.seed_secrets(cfg, tmp_path, {})
    assert not any("create-secret" in c for c in calls)


def test_seed_secrets_db_missing_fails(monkeypatch, tmp_path):
    """§3-4 — /db 는 terraform 소유. 없으면 수동 생성이 아니라 중단."""
    cfg = _cfg()
    monkeypatch.setattr(bootstrap, "secret_prefix", lambda c, r: "/llm-gateway/")
    fake, _ = _aws_router({})
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    monkeypatch.setattr(bootstrap, "_aws", lambda *a, **k: _cp(0))
    with pytest.raises(SystemExit, match="terraform"):
        bootstrap.seed_secrets(cfg, tmp_path, {})


def test_seed_secrets_db_incomplete_fails(monkeypatch, tmp_path):
    cfg = _cfg()
    monkeypatch.setattr(bootstrap, "secret_prefix", lambda c, r: "/llm-gateway/")
    fake, _ = _aws_router({
        "/llm-gateway/dev/app": {"x": 1},          # 이미 존재 → 생성 생략
        "/llm-gateway/dev/redis": {"password": "x"},
        "/llm-gateway/dev/db": {"password": "p"},  # master_password 누락
    })
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    with pytest.raises(SystemExit, match="master_password"):
        bootstrap.seed_secrets(cfg, tmp_path, {})


# ── 이미지 (§3-5) ─────────────────────────────────────────────────────────────

def test_ensure_ecr_repos_tolerates_existing(monkeypatch):
    monkeypatch.setattr(
        bootstrap.subprocess, "run",
        lambda *a, **k: _cp(1, err="RepositoryAlreadyExistsException"))
    bootstrap.ensure_ecr_repos("ap-northeast-2")


def test_ensure_ecr_repos_fails_on_other_errors(monkeypatch):
    monkeypatch.setattr(bootstrap.subprocess, "run",
                        lambda *a, **k: _cp(1, err="AccessDenied"))
    with pytest.raises(SystemExit, match="ECR"):
        bootstrap.ensure_ecr_repos("ap-northeast-2")


def test_push_images_tag_per_service(monkeypatch, tmp_path):
    builds = []
    monkeypatch.setattr(bootstrap, "_run",
                        lambda argv, **k: builds.append(argv))
    monkeypatch.setattr(bootstrap, "ensure_ecr_repos", lambda r: None)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: _cp(0, "pw"))
    cfg = _cfg(images={"tag": "v1",
                       "tags": {"gatewayProxy": "gw-9", "migration": "mig-7"}})
    bootstrap.push_images_eks(cfg, {}, tmp_path, "ap-northeast-2")
    tags = [a[-2] for a in builds if a[1] == "build"]
    assert "123456789012.dkr.ecr.ap-northeast-2.amazonaws.com/llm-gateway/gateway-proxy:gw-9" in tags
    assert any(i.endswith("migration:mig-7") for i in tags)
    # migration 컨텍스트는 ./db
    mig_build = next(a for a in builds if a[1] == "build" and "migration" in a[-2])
    assert mig_build[-1].endswith("/db")


def test_push_images_requires_tag(monkeypatch, tmp_path):
    monkeypatch.setattr(bootstrap, "_run", lambda *a, **k: None)
    monkeypatch.setattr(bootstrap, "ensure_ecr_repos", lambda r: None)
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **k: _cp(0, "pw"))
    cfg = _cfg()
    cfg.images.tag = ""          # 스키마 검증 이후 비우기 — 런타임 방어 경로
    cfg.images.tags = {}
    with pytest.raises(SystemExit, match="images.tag"):
        bootstrap.push_images_eks(cfg, {}, tmp_path, "ap-northeast-2")


# ── cognito (§3-8) ────────────────────────────────────────────────────────────

def test_onboard_admin_creates_and_groups(monkeypatch, tmp_path):
    created = []
    def fake(cmd, **kw):
        if "admin-get-user" in cmd:
            return _cp(1)     # 없음 → 생성
        created.append(cmd)
        return _cp(0)
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    monkeypatch.setattr(bootstrap, "_aws", lambda args, region, **k: (
        created.append(args), _cp(0))[1])
    vals = tmp_path / "deployment/charts/llm-gateway/values-eks-fargate-dev.yaml"
    vals.parent.mkdir(parents=True)
    vals.write_text("adminApi:\n  adminBootstrap:\n    emails: [admin@corp.com]\n")
    notes = bootstrap.onboard_admin(
        _cfg(), tmp_path, {"cognito_user_pool_id": "pool-1"}, "ap-northeast-2")
    assert any("admin-create-user" in c for c in created)
    assert any("admin-add-user-to-group" in c for c in created)
    assert any("임시 비밀번호" in n for n in notes)


def test_onboard_admin_existing_user_skips_create(monkeypatch, tmp_path):
    created = []
    def fake(cmd, **kw):
        created.append(cmd)
        return _cp(0)   # admin-get-user 성공 → 이미 존재
    monkeypatch.setattr(bootstrap.subprocess, "run", fake)
    vals = tmp_path / "deployment/charts/llm-gateway/values-eks-fargate-dev.yaml"
    vals.parent.mkdir(parents=True)
    vals.write_text("adminApi:\n  adminBootstrap:\n    emails: [admin@corp.com]\n")
    notes = bootstrap.onboard_admin(
        _cfg(), tmp_path, {"cognito_user_pool_id": "pool-1"}, "ap-northeast-2")
    assert not any("admin-create-user" in c for c in created)
    assert any("이미 존재" in n for n in notes)
