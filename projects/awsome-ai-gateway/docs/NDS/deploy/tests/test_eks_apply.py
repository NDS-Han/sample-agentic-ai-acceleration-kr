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
