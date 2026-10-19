# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""websearch — AgentCore Gateway 프로비저닝 래퍼 + apply 배선 단위 테스트."""
import subprocess

from deploy import websearch, eks_apply, ecs_apply


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


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


# ── websearch 단위 ────────────────────────────────────────────────────────────

def test_status_url_parses_and_normalizes_mcp(monkeypatch, tmp_path):
    out = "gateway: g1 status=READY\n  url: https://g1.gateway.bedrock-agentcore.us-east-1.amazonaws.com\n  target: t status=READY\n"
    monkeypatch.setattr(websearch, "_run", lambda *a, **k: _cp(0, out))
    url = websearch.status_url("dev", tmp_path)
    assert url.endswith("/mcp")


def test_status_url_missing_gateway(monkeypatch, tmp_path):
    monkeypatch.setattr(websearch, "_run", lambda *a, **k: _cp(0, "gateway: NONE\n"))
    assert websearch.status_url("dev", tmp_path) == ""


def test_ensure_reuses_existing_without_deploy(monkeypatch, tmp_path):
    calls = []
    def fake(env_name, repo_root, sub, caller_role_arn=""):
        calls.append(sub)
        return _cp(0, "  url: https://g/mcp\n")
    monkeypatch.setattr(websearch, "_run", fake)
    assert websearch.ensure_gateway_url("dev", tmp_path) == "https://g/mcp"
    assert calls == ["status"]        # deploy 는 돌리지 않는다


def test_ensure_provisions_when_missing(monkeypatch, tmp_path):
    def fake(env_name, repo_root, sub, caller_role_arn=""):
        if sub == "status":
            return _cp(0, "gateway: NONE\n")
        return _cp(0, "AGENTCORE_GATEWAY_URL=https://g2.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp\n")
    monkeypatch.setattr(websearch, "_run", fake)
    url = websearch.ensure_gateway_url("dev", tmp_path)
    assert url == "https://g2.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp"


def test_ensure_failure_returns_empty_not_exception(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(websearch, "_run", lambda *a, **k: _cp(1, "", "boto3 missing"))
    assert websearch.ensure_gateway_url("dev", tmp_path) == ""
    assert "프로비저닝 실패" in capsys.readouterr().err


def test_gw_name_matches_set_websearch_url_sh_convention(tmp_path):
    """set-websearch-url.sh 는 GW_NAME=llm-gateway-websearch-<env> — 같지 않으면
    수동 배포의 gateway 를 재사용하지 못하고 중복 생성된다."""
    e = websearch._env("prod", "arn:aws:iam::1:role/x")
    assert e["GW_NAME"] == "llm-gateway-websearch-prod"
    assert e["REGION"] == "us-east-1"
    assert e["CALLER_ROLE_ARN"] == "arn:aws:iam::1:role/x"


def test_upsert_env_file_replaces_and_appends(tmp_path):
    p = tmp_path / ".env"
    p.write_text("A=1\nAGENTCORE_GATEWAY_URL=old\n")
    websearch.upsert_env_file(p, {"AGENTCORE_GATEWAY_URL": "new", "AGENTCORE_REGION": "us-east-1"})
    txt = p.read_text()
    assert "A=1" in txt
    assert "AGENTCORE_GATEWAY_URL=new" in txt and "old" not in txt
    assert "AGENTCORE_REGION=us-east-1" in txt


# ── eks apply 배선 ────────────────────────────────────────────────────────────

def test_eks_apply_injects_agentcore_url(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "load_meta", lambda g, r: {
        "release": "r", "namespace": "n", "chart": "c",
        "env_dir": "", "values_layers": []})
    monkeypatch.setattr(eks_apply, "live_alb_hostname", lambda m, c="": "h.elb")
    monkeypatch.setattr(eks_apply.websearch, "ensure_gateway_url",
                        lambda *a, **k: "https://g.gateway.bedrock-agentcore.us-east-1.amazonaws.com/mcp")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "web_search": True})
    eks_apply.apply(cfg, tmp_path, tmp_path)
    joined = " ".join(calls[0])
    assert "AGENTCORE_GATEWAY_URL=https://g.gateway" in joined
    assert "AGENTCORE_REGION=us-east-1" in joined
    assert "AGENTCORE_TARGET_ID=web-search-tool" in joined


def test_eks_apply_websearch_off_adds_nothing(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "load_meta", lambda g, r: {
        "release": "r", "namespace": "n", "chart": "c",
        "env_dir": "", "values_layers": []})
    monkeypatch.setattr(eks_apply, "live_alb_hostname", lambda m, c="": "h.elb")
    monkeypatch.setattr(eks_apply.websearch, "ensure_gateway_url",
                        lambda *a, **k: "https://g/mcp")
    eks_apply.apply(_cfg(), tmp_path, tmp_path)   # web_search 기본 off
    assert not any("AGENTCORE" in a for a in calls[0])


def test_eks_apply_websearch_provision_failure_continues(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "load_meta", lambda g, r: {
        "release": "r", "namespace": "n", "chart": "c",
        "env_dir": "", "values_layers": []})
    monkeypatch.setattr(eks_apply, "live_alb_hostname", lambda m, c="": "h.elb")
    monkeypatch.setattr(eks_apply.websearch, "ensure_gateway_url", lambda *a, **k: "")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "web_search": True})
    eks_apply.apply(cfg, tmp_path, tmp_path)
    assert not any("AGENTCORE" in a for a in calls[0])
    assert "URL 을 얻지 못" in capsys.readouterr().err


# ── ecs apply 배선 ────────────────────────────────────────────────────────────

def _ecs_apply_stubs(monkeypatch):
    monkeypatch.setattr(ecs_apply, "terraform_apply", lambda *a, **k: {})
    monkeypatch.setattr(ecs_apply, "tf_outputs", lambda d: {})
    monkeypatch.setattr(ecs_apply, "push_images", lambda *a, **k: None)
    monkeypatch.setattr(ecs_apply, "wait_services", lambda *a, **k: None)


def test_ecs_apply_websearch_into_extra_vars(monkeypatch, tmp_path):
    saved, rendered = {}, []
    monkeypatch.setattr(ecs_apply, "load_extra_vars", lambda g: {})
    monkeypatch.setattr(ecs_apply, "_save_extra_vars",
                        lambda g, e: saved.update(e))
    _ecs_apply_stubs(monkeypatch)
    import deploy.render.ecs as ecs_render
    import deploy.websearch as ws_mod
    monkeypatch.setattr(ecs_render, "render",
                        lambda cfg, g, extra_vars=None: rendered.append(extra_vars))
    monkeypatch.setattr(ws_mod, "ensure_gateway_url",
                        lambda *a, **k: "https://g/mcp")
    cfg = _cfg(deploy={"target": "ecs", "size_tier": "t1"},
               features={"notifications": {"provider": "mock"},
                         "web_search": True})
    ecs_apply.apply(cfg, tmp_path, tmp_path)
    assert saved["agentcore_gateway_url"] == "https://g/mcp"
    assert rendered[0]["agentcore_gateway_url"] == "https://g/mcp"


def test_ecs_apply_websearch_off_removes_stale_url(monkeypatch, tmp_path):
    saved = {}
    monkeypatch.setattr(ecs_apply, "load_extra_vars",
                        lambda g: {"agentcore_gateway_url": "https://old/mcp"})
    monkeypatch.setattr(ecs_apply, "_save_extra_vars",
                        lambda g, e: saved.update(e))
    _ecs_apply_stubs(monkeypatch)
    import deploy.render.ecs as ecs_render
    monkeypatch.setattr(ecs_render, "render", lambda *a, **k: None)
    cfg = _cfg(deploy={"target": "ecs", "size_tier": "t1"})
    ecs_apply.apply(cfg, tmp_path, tmp_path)
    # 선언 off → 잔여 URL 제거 (taskdef 의 url!="" → WEB_SEARCH_ENABLED=true 되돌림)
    assert saved.get("agentcore_gateway_url") is None
