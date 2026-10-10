# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import yaml

from deploy import schema
from deploy.render import compose, common


def cfg(**over):
    doc = {"version": 1, "env": "acme-test",
           "deploy": {"target": "compose", "size_tier": "t0"}}
    doc.update(over)
    return schema.from_dict(doc)


def render_to(tmp_path, config):
    return compose.render(config, tmp_path), yaml.safe_load(
        (tmp_path / "docker-compose.yml").read_text())


def test_drops_mock_and_observability(tmp_path):
    _, out = render_to(tmp_path, cfg())
    svc = out["services"]
    assert "mock-vllm" not in svc
    for s in ("prometheus", "loki", "tempo", "grafana", "otel-collector"):
        assert s not in svc
    assert "caddy" in svc
    # OTEL env 도 함께 제거됐는가
    for name, s in svc.items():
        env = s.get("environment") or {}
        assert not any(k.startswith("OTEL_") for k in env), name


def test_observability_profile_keeps_stack(tmp_path):
    _, out = render_to(tmp_path, cfg(features={"observability": True}))
    assert "grafana" in out["services"]
    assert "mock-vllm" not in out["services"]  # mock 은 언제나 제거


def test_no_host_ports_except_caddy(tmp_path):
    _, out = render_to(tmp_path, cfg())
    for name, s in out["services"].items():
        if name == "caddy":
            continue
        assert "ports" not in s, f"{name} 에 포트가 노출됐다"


def test_images_pin_replaces_build(tmp_path):
    _, out = render_to(tmp_path, cfg(
        images={"registry": "111.dkr.ecr.ap-northeast-2.amazonaws.com/gw", "tag": "abc123"}))
    gw = out["services"]["gateway-proxy"]
    assert "build" not in gw
    assert gw["image"].endswith("gateway-proxy:abc123")
    # scheduler 는 admin-api 이미지를 쓴다
    assert out["services"]["scheduler"]["image"].endswith("admin-api:abc123")


def test_no_image_tag_keeps_build(tmp_path):
    _, out = render_to(tmp_path, cfg())
    assert "build" in out["services"]["gateway-proxy"]


def test_mounts_and_context_absolutized(tmp_path):
    _, out = render_to(tmp_path, cfg())
    for s in out["services"].values():
        for v in s.get("volumes") or []:
            if isinstance(v, str) and ":" in v:
                src = v.split(":")[0]
                # ./Caddyfile 등 생성 디렉토리(gen/<env>) 내부 파일은 의도된 상대경로
                if src.startswith("./"):
                    assert src in ("./Caddyfile",), v
        b = s.get("build")
        if isinstance(b, dict):
            assert not str(b.get("context", "")).startswith(".")


def test_caddyfile_no_domain_port_routing(tmp_path):
    render_to(tmp_path, cfg())
    body = (tmp_path / "Caddyfile").read_text()
    assert ":8000" in body and "gateway-proxy:8000" in body
    assert "admin-ui:3000" in body


def test_caddyfile_domain_host_routing(tmp_path):
    render_to(tmp_path, cfg(domain={"mode": "route53-acm", "name": "example.com"}))
    body = (tmp_path / "Caddyfile").read_text()
    assert "gateway.example.com" in body
    assert "admin.example.com" in body
    assert "admin-api.example.com" in body


def test_caddyfile_ip_allowlist(tmp_path):
    render_to(tmp_path, cfg(network={"mode": "public", "allowed_cidrs": ["10.0.0.0/8"]}))
    body = (tmp_path / "Caddyfile").read_text()
    assert "remote_ip 10.0.0.0/8" in body
    assert "403" in body


def test_env_preserves_secrets_on_rerender(tmp_path):
    render_to(tmp_path, cfg())
    first = common.parse_env_file(tmp_path / ".env")
    render_to(tmp_path, cfg())
    second = common.parse_env_file(tmp_path / ".env")
    assert first["VIRTUAL_KEY_ENCRYPTION_KEY"] == second["VIRTUAL_KEY_ENCRYPTION_KEY"]
    assert first["POSTGRES_PASSWORD"] == second["POSTGRES_PASSWORD"]


def test_env_generated_keys_filled(tmp_path):
    render_to(tmp_path, cfg())
    env = common.parse_env_file(tmp_path / ".env")
    assert len(env["VIRTUAL_KEY_ENCRYPTION_KEY"]) == 64
    assert env["DEV_LOGIN_ENABLED"] == "true"   # oidc 비활성 → dev-login 필요
    assert env["POSTGRES_PASSWORD"] != "gateway_dev_password"


def test_dev_login_off_when_oidc(tmp_path):
    render_to(tmp_path, cfg(oidc={
        "issuer_url": "https://idp.example.com", "client_id": "c",
        "authorize_url": "https://a", "token_url": "https://t"}))
    env = common.parse_env_file(tmp_path / ".env")
    assert env["DEV_LOGIN_ENABLED"] == "false"
    assert env["OIDC_ISSUER_URL"] == "https://idp.example.com"
    # admin-ui 는 env_file 을 안 읽으므로 service environment 에 주입돼야 한다
    out = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    ui_env = out["services"]["admin-ui"]["environment"]
    assert ui_env["OIDC_CLIENT_ID"] == "c"


def test_env_0600(tmp_path):
    render_to(tmp_path, cfg())
    import os, stat
    mode = stat.S_IMODE(os.stat(tmp_path / ".env").st_mode)
    assert mode == 0o600


def test_nextauth_url_only_with_domain(tmp_path):
    _, out = render_to(tmp_path, cfg())
    ui_env = out["services"]["admin-ui"]["environment"]
    assert "NEXTAUTH_URL" not in ui_env  # 도메인 없으면 Host 헤더 유도 (localhost 단정 금지)
    _, out2 = render_to(tmp_path / "d2", cfg(domain={"mode": "route53-acm", "name": "example.com"}))
    assert out2["services"]["admin-ui"]["environment"]["NEXTAUTH_URL"] == "https://admin.example.com"


def test_caddy_waits_for_health(tmp_path):
    _, out = render_to(tmp_path, cfg())
    deps = out["services"]["caddy"]["depends_on"]
    assert deps["gateway-proxy"]["condition"] == "service_healthy"


def test_oidc_client_secret_interpolated(tmp_path):
    render_to(tmp_path, cfg(oidc={
        "issuer_url": "https://idp.example.com", "client_id": "c",
        "client_secret": "s3cret",
        "authorize_url": "https://a", "token_url": "https://t"}))
    out = yaml.safe_load((tmp_path / "docker-compose.yml").read_text())
    ui_env = out["services"]["admin-ui"]["environment"]
    # 시크릿 값 자체는 compose 에 박지 않고 .env interpolate
    assert ui_env["OIDC_CLIENT_SECRET"] == "${OIDC_CLIENT_SECRET:-}"
    env = common.parse_env_file(tmp_path / ".env")
    assert env["OIDC_CLIENT_SECRET"] == "s3cret"


def test_unimplemented_features_noted_not_silent(tmp_path):
    res, _ = render_to(tmp_path, cfg(features={
        "web_search": True, "body_logging": True, "pricing_lambda": True}))
    notes = res["notes"]
    assert any("web_search" in n and "AGENTCORE_GATEWAY_URL" in n for n in notes)
    assert any("body_logging" in n and "미지원" in n for n in notes)
    assert any("pricing_lambda" in n for n in notes)
    # web_search env 는 써 둔다 — URL/DB 행은 수동 프로비저닝
    env = common.parse_env_file(tmp_path / ".env")
    assert env["WEB_SEARCH_ENABLED"] == "true"


def test_grafana_volume_dropped_when_no_observability(tmp_path):
    _, out = render_to(tmp_path, cfg())
    assert "grafana-data" not in out["volumes"]
    assert "pgdata" in out["volumes"]


def test_grafana_volume_kept_with_observability(tmp_path):
    _, out = render_to(tmp_path, cfg(features={"observability": True}))
    assert "grafana-data" in out["volumes"]
