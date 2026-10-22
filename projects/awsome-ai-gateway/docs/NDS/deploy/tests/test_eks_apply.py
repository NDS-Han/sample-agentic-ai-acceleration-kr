# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks_apply — helm/kubectl 호출 경계 단위 테스트."""
import json
import subprocess

from deploy import eks_apply


def _cp(items):
    return subprocess.CompletedProcess([], 0, json.dumps({"items": items}), "")


def _ingress(name, hostname=None):
    item = {"metadata": {"name": name}, "status": {}}
    if hostname:
        item["status"] = {"loadBalancer": {"ingress": [{"hostname": hostname}]}}
    return item


def test_live_alb_hostname_picks_admin_ui_not_first(monkeypatch):
    """kubectl 리스트는 이름순이라 -admin-api 가 먼저 온다. 첫 hostname 을 집으면
    NEXTAUTH_URL 이 admin-api ALB 를 가리켜 OIDC 콜백이 깨진다 — 이름으로 골라야 한다."""
    items = [
        _ingress("llm-gateway-admin-api", "admin-api-123.elb.amazonaws.com"),
        _ingress("llm-gateway-admin-ui", "admin-ui-456.elb.amazonaws.com"),
        _ingress("llm-gateway-gateway", "gw-789.elb.amazonaws.com"),
    ]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    assert (eks_apply.live_alb_hostname({"namespace": "ns"})
            == "admin-ui-456.elb.amazonaws.com")


def test_live_alb_hostname_custom_release_suffix(monkeypatch):
    # fullnameOverride 로 이름이 달라도 -admin-ui 접미사는 유지된다
    items = [_ingress("custom-admin-ui", "ui.elb.amazonaws.com")]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    assert eks_apply.live_alb_hostname({"namespace": "ns"}) == "ui.elb.amazonaws.com"


def test_live_alb_hostname_skips_admin_ui_without_ip(monkeypatch):
    # admin-ui 의 ALB 가 아직 안 떴는데 다른 것만 뜬 상태 → "" (재시도 유도)
    items = [
        _ingress("llm-gateway-admin-api", "admin-api.elb.amazonaws.com"),
        _ingress("llm-gateway-admin-ui"),   # hostname 아직 없음
        _ingress("llm-gateway-gateway", "gw.elb.amazonaws.com"),
    ]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    assert eks_apply.live_alb_hostname({"namespace": "ns"}) == ""


def test_live_alb_hostname_kubectl_failure(monkeypatch):
    r = subprocess.CompletedProcess([], 1, "", "connection refused")
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: r)
    assert eks_apply.live_alb_hostname({"namespace": "ns"}) == ""


def test_context_flag_propagates(monkeypatch):
    seen = {}
    def fake(argv, **k):
        seen["argv"] = argv
        return _cp([])
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    eks_apply.live_alb_hostname({"namespace": "ns"}, context="prod-ctx")
    assert "--context" in seen["argv"] and "prod-ctx" in seen["argv"]


def _cfg(**over):
    from deploy import schema
    doc = {
        "version": 1, "env": "dev",
        "aws": {"region": "ap-northeast-2"},
        "deploy": {"target": "eks", "size_tier": "t3"},
        "features": {"notifications": {"provider": "mock"}},
        "images": {"tag": "v1"},
    }
    doc.update(over)
    return schema.from_dict(doc)


def test_ses_notification_wires_worker_irsa(monkeypatch, capsys):
    """install-eks.sh R24-S7 계약 — provider=ses 이면 notification-worker SA 에
    SES IRSA 가 붙어야 한다. 없으면 발송이 전량 실패."""
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    cfg = _cfg(features={"notifications": {"provider": "ses", "ses_from": "a@b.c"}})
    outs = {"notification_worker_ses_role_arn": "arn:aws:iam::1:role/ses"}
    args = eks_apply.dynamic_set_args(cfg, outs, "ap-northeast-2")
    joined = " ".join(args)
    assert "notificationWorker.serviceAccount" in joined
    assert "arn:aws:iam::1:role/ses" in joined


def test_ses_notification_missing_role_warns(monkeypatch, capsys):
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    cfg = _cfg(features={"notifications": {"provider": "ses", "ses_from": "a@b.c"}})
    args = eks_apply.dynamic_set_args(cfg, {}, "ap-northeast-2")
    assert not any("notificationWorker" in a for a in args)
    assert "notification_worker_ses_role_arn" in capsys.readouterr().err


def test_mock_notification_skips_worker_irsa(monkeypatch):
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    args = eks_apply.dynamic_set_args(_cfg(), {}, "ap-northeast-2")
    assert not any("notificationWorker" in a for a in args)


def test_dry_run_flag_server_for_helm_313(monkeypatch):
    monkeypatch.setattr(eks_apply, "_helm_version", lambda: (3, 14))
    assert eks_apply._dry_run_flag() == "--dry-run=server"


def test_dry_run_flag_legacy_before_313(monkeypatch):
    monkeypatch.setattr(eks_apply, "_helm_version", lambda: (3, 10))
    assert eks_apply._dry_run_flag() == "--dry-run"


def test_dry_run_flag_server_on_helm4(monkeypatch):
    monkeypatch.setattr(eks_apply, "_helm_version", lambda: (4, 0))
    assert eks_apply._dry_run_flag() == "--dry-run=server"


# ==============================================================================
# ensure_infra — 선언된 기능 인프라의 targeted terraform apply
# ==============================================================================

def _env_dir(tmp_path):
    d = tmp_path / "env"
    d.mkdir()
    return d


def test_ensure_infra_targeted_apply_when_body_log_missing(monkeypatch, tmp_path):
    """body_logging=true 인데 output 이 없으면 ops 8-V 와 같은 targeted apply."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"body_log_firehose_stream": "s",
                                   "body_log_bucket": "b"})
    (tmp_path / "terraform.tfvars").write_text("enable_body_logging = true\n")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), {})
    assert outs["body_log_bucket"] == "b"
    assert calls[0][:2] == ["terraform", "init"]
    apply_argv = calls[1]
    assert apply_argv[:2] == ["terraform", "apply"]
    assert "-target=module.body_logging" in apply_argv
    assert "-target=module.irsa" in apply_argv
    assert any(a.startswith("-var-file=") for a in apply_argv)


def test_ensure_infra_skips_when_outputs_present(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "b"}
    assert eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), outs) is outs
    assert calls == []


def test_ensure_infra_ses_targets_irsa_only(monkeypatch, tmp_path):
    """provider=ses 인데 worker IRSA output 이 없으면 module.irsa 만."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"notification_worker_ses_role_arn": "arn:r"})
    cfg = _cfg(features={"notifications": {"provider": "ses",
                                           "ses_from": "a@b.c"}})
    eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), {})
    apply_argv = calls[1]
    assert "-target=module.irsa" in apply_argv
    assert not any("body_logging" in a for a in apply_argv)


def test_ensure_infra_noop_when_features_off(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    assert eks_apply.ensure_infra(_cfg(), tmp_path, _env_dir(tmp_path), {}) == {}
    assert calls == []


def test_ensure_infra_missing_env_dir_warns(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = eks_apply.ensure_infra(cfg, tmp_path, tmp_path / "nope", {})
    assert outs == {} and calls == []
    assert "terraform env" in capsys.readouterr().err


def test_live_ingress_rule_host_reads_spec_rules(monkeypatch):
    item = _ingress("llm-gateway-admin-ui")
    item["spec"] = {"rules": [{"host": "admin-dev.example.com"}]}
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp([item]))
    assert (eks_apply.live_ingress_rule_host({"namespace": "ns"})
            == "admin-dev.example.com")


def test_live_ingress_rule_host_empty_on_fresh_install(monkeypatch):
    # domain=none 신규 설치 — rule host 없음 → ALB 2-phase 가 필요
    item = _ingress("llm-gateway-admin-ui")
    item["spec"] = {"rules": [{}]}
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp([item]))
    assert eks_apply.live_ingress_rule_host({"namespace": "ns"}) == ""


def _apply_harness(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "tf_outputs", lambda d: {})
    monkeypatch.setattr(eks_apply, "ensure_infra",
                        lambda cfg, gen, env, outs: outs)
    monkeypatch.setattr(eks_apply, "load_meta", lambda g, r: {
        "release": "r", "namespace": "n", "chart": "c",
        "env_dir": "env", "values_layers": []})
    return calls


def test_apply_skips_alb_2phase_when_ingress_host_exists(monkeypatch, tmp_path):
    """채택 배포 — env values 가 admin-dev.example.com host 를 공급하면 차트가
    https://<host> 를 파생한다. ALB 주입을 하면 콜백이 깨지므로 생략해야 한다."""
    calls = _apply_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(eks_apply, "live_ingress_rule_host",
                        lambda m, context="": "admin-dev.example.com")
    monkeypatch.setattr(eks_apply, "live_alb_hostname",
                        lambda m, context="": "alb-123.elb.amazonaws.com")
    eks_apply.apply(_cfg(), tmp_path, tmp_path)
    assert len(calls) == 1  # 2차 upgrade 없음
    assert not any("nextauthUrl" in a for c in calls for a in c)


def test_apply_alb_2phase_on_fresh_install(monkeypatch, tmp_path):
    """domain=none 신규 설치 — rule host 가 없으면 ALB DNS 를 nextauthUrl 로 주입."""
    calls = _apply_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(eks_apply, "live_ingress_rule_host",
                        lambda m, context="": "")
    monkeypatch.setattr(eks_apply, "live_alb_hostname",
                        lambda m, context="": "alb-123.elb.amazonaws.com")
    eks_apply.apply(_cfg(), tmp_path, tmp_path)
    assert len(calls) == 2
    assert "adminUi.nextauthUrl=http://alb-123.elb.amazonaws.com" in calls[1]


def test_plan_never_runs_terraform_apply(monkeypatch, tmp_path, capsys):
    """plan 은 읽기 전용 — 인프라가 비어 있어도 terraform apply 를 돌리지 않는다."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "tf_outputs", lambda d: {})
    monkeypatch.setattr(eks_apply, "load_meta", lambda g, r: {
        "release": "r", "namespace": "n", "chart": "c",
        "env_dir": "env", "values_layers": []})
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    eks_apply.plan(cfg, tmp_path, tmp_path)
    assert calls and calls[0][0] == "helm"
    assert "--dry-run" in calls[0] or "--dry-run=server" in calls[0]
    assert not any("apply" in a for a in calls[0])
    assert "targeted terraform apply" in capsys.readouterr().err
