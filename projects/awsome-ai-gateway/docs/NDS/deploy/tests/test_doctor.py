# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""doctor 순수 함수 — per-service 태그 기대값 계산."""
from deploy import doctor, schema


def _cfg(images):
    return schema.from_dict({
        "version": 1, "env": "dev",
        "deploy": {"target": "eks", "size_tier": "t3"},
        "images": images})


def test_expected_tag_single_tag_all_services():
    cfg = _cfg({"tag": "abc123"})
    assert doctor._expected_tag(cfg, "gateway-proxy") == "abc123"
    assert doctor._expected_tag(cfg, "admin-api") == "abc123"


def test_expected_tag_per_service_pin_overrides():
    cfg = _cfg({"tag": "abc123", "tags": {"adminApi": "other-tag"}})
    assert doctor._expected_tag(cfg, "admin-api") == "other-tag"
    assert doctor._expected_tag(cfg, "gateway-proxy") == "abc123"


def test_expected_tag_tags_only_no_default():
    cfg = _cfg({"tags": {"gatewayProxy": "pin-a"}})
    assert doctor._expected_tag(cfg, "gateway-proxy") == "pin-a"
    # 핀 없는 서비스는 기대값 없음 → drift 검사 생략 (env overlay 태그)
    assert doctor._expected_tag(cfg, "admin-ui") == ""


def test_expected_tag_migration_not_a_deployment():
    # migration 은 EKS_DEPLOYMENTS 에 없고 kebab 매핑 대상도 아니다 —
    # images.tags.migration 핀이 서비스 기대값으로 새어나가면 안 된다
    cfg = _cfg({"tag": "abc", "tags": {"migration": "mig-tag"}})
    assert doctor._expected_tag(cfg, "gateway-proxy") == "abc"


def test_tf_drift_plan_uses_managed_overlay_when_present(tmp_path):
    # overlay 가 있으면 apply 와 같은 -var-file 을 붙인다 — 없으면 env tfvars 만.
    (tmp_path / "eks").mkdir()
    argv = doctor._tf_drift_plan_argv(tmp_path)
    assert not any(a.startswith("-var-file=") for a in argv)
    (tmp_path / "eks" / "terraform.tfvars").write_text("x = 1\n")
    argv = doctor._tf_drift_plan_argv(tmp_path)
    assert any(a.endswith("eks/terraform.tfvars") for a in argv
               if a.startswith("-var-file="))
    assert "-no-color" in argv


# ==============================================================================
# R18 회귀 — compose migration ExitCode / eks 컨테이너 선택
# ==============================================================================

import json, subprocess
from pathlib import Path


def test_compose_migration_exit_nonzero_is_high(tmp_path, monkeypatch):
    """exited 상태만 보면 exit=1 실패 마이그레이션이 '완료'로 오진된다."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=x\nVIRTUAL_KEY_ENCRYPTION_KEY=k\n"
        "NEXTAUTH_SECRET=n\nINTERNAL_API_TOKEN=t\n")
    (tmp_path / "Caddyfile").write_text(":8000 {\n}\n")
    rows = [{"Service": "migration", "State": "exited", "ExitCode": 1}]
    calls = {"n": 0}

    def fake_run(argv, **kw):
        calls["n"] += 1
        if "ps" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(rows), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(doctor, "_run", fake_run)
    rep = doctor.check_compose(
        schema.from_dict({"version": 1, "env": "dev",
                          "deploy": {"target": "compose", "size_tier": "t0"}}),
        tmp_path)
    f = [x for x in rep.findings if x.check == "services"
         and "migration" in x.message]
    assert f and f[0].level == "HIGH"


def test_compose_migration_exit_zero_is_ok(tmp_path, monkeypatch):
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".env").write_text(
        "POSTGRES_PASSWORD=x\nVIRTUAL_KEY_ENCRYPTION_KEY=k\n"
        "NEXTAUTH_SECRET=n\nINTERNAL_API_TOKEN=t\n")
    (tmp_path / "Caddyfile").write_text(":8000 {\n}\n")
    rows = [{"Service": "migration", "State": "exited", "ExitCode": 0}]
    monkeypatch.setattr(
        doctor, "_run",
        lambda argv, **kw: subprocess.CompletedProcess(
            argv, 0, json.dumps(rows) if "ps" in argv else "", ""))
    rep = doctor.check_compose(
        schema.from_dict({"version": 1, "env": "dev",
                          "deploy": {"target": "compose", "size_tier": "t0"}}),
        tmp_path)
    f = [x for x in rep.findings if x.check == "services"
         and "migration" in x.message]
    assert f and f[0].level == "OK"
