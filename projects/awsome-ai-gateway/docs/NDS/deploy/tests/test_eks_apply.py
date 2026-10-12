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
