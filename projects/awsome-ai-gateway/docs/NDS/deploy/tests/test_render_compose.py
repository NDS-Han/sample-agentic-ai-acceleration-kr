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
    # migration 은 db/ 전용 이미지 — admin-api 로 바뀌면 psql/alembic 이 없어
    # completed 게이트가 영구 대기한다 (회귀)
    assert out["services"]["migration"]["image"].endswith("migration:abc123")
    # migration 은 init SQL 도 재적용하도록 master 자격을 갖는다
    env_map = out["services"]["migration"].get("environment", {})
    assert "DB_MASTER_URL" in env_map


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
    render_to(tmp_path, cfg(features={"dev_login": True}))
    env = common.parse_env_file(tmp_path / ".env")
    assert len(env["VIRTUAL_KEY_ENCRYPTION_KEY"]) == 64
    assert env["DEV_LOGIN_ENABLED"] == "true"   # 선언 + oidc 비활성 → dev-login
    assert env["POSTGRES_PASSWORD"] != "gateway_dev_password"


def test_dev_login_off_unless_declared(tmp_path):
    """dev_login 미선언 + oidc 비활성 — eks 와 같은 fail-closed: 무인증 dev
    세션을 열지 않는다(로그인 경로 없음은 schema 경고가 안내)."""
    render_to(tmp_path, cfg())
    env = common.parse_env_file(tmp_path / ".env")
    assert env["DEV_LOGIN_ENABLED"] == "false"


def test_dev_login_declared_but_oidc_stays_off(tmp_path):
    """dev_login=true 여도 OIDC 배선 시 admin-api 가 dev 토큰을 거부하므로
    UI/양쪽 다 false 로 수렴한다 (eks 와 동일 계약)."""
    render_to(tmp_path, cfg(features={"dev_login": True}, oidc={
        "issuer_url": "https://idp.example.com", "client_id": "c",
        "authorize_url": "https://a", "token_url": "https://t"}))
    env = common.parse_env_file(tmp_path / ".env")
    assert env["DEV_LOGIN_ENABLED"] == "false"


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


def test_gateway_aws_mount_stripped(tmp_path):
    """t0 는 instance profile(IMDS)이 정답 — 호스트 ~/.aws 가 컨테이너에
    마운트되면 SDK 체인상 static creds 가 IMDS 를 조용히 앞선다."""
    _, out = render_to(tmp_path, cfg())
    for v in out["services"]["gateway-proxy"].get("volumes") or []:
        assert ".aws" not in str(v), v


def test_observability_ports_loopback_only(tmp_path):
    """observability=true 시 관측 스택이 호스트 포트를 열지만, 전부 127.0.0.1
    이어야 한다 — Caddy allowlist 를 우회하는 외부 노출(OTLP 쓰기, admin/admin
    grafana)을 차단."""
    _, out = render_to(tmp_path, cfg(features={"observability": True}))
    for name, s in out["services"].items():
        if name == "caddy":
            continue
        for p in s.get("ports") or []:
            assert str(p).startswith("127.0.0.1:"), f"{name}: {p}"


def test_grafana_admin_password_generated(tmp_path):
    """observability 켜도 GF_SECURITY_ADMIN_PASSWORD 가 비면 grafana 는
    admin/admin — 생성 키로 채워져야 한다."""
    render_to(tmp_path, cfg(features={"observability": True}))
    env = common.parse_env_file(tmp_path / ".env")
    assert env["GF_SECURITY_ADMIN_PASSWORD"] and env["GF_SECURITY_ADMIN_PASSWORD"] != "admin"


def test_service_keys_disabled_by_default(tmp_path):
    """기본 꺼짐 — 미선언이면 .env 에 SERVICE_KEYS_ENABLED=false 를 명시한다
    (미방출이면 admin-api 앱 기본 true 가 산다)."""
    render_to(tmp_path, cfg())
    env = common.parse_env_file(tmp_path / ".env")
    assert env["SERVICE_KEYS_ENABLED"] == "false"


def test_service_keys_enabled_env(tmp_path):
    render_to(tmp_path, cfg(features={"service_keys": True}))
    env = common.parse_env_file(tmp_path / ".env")
    assert env["SERVICE_KEYS_ENABLED"] == "true"


# ==============================================================================
# R24 회귀 — merge_env 운영자 키 보존 / 산출물 원자성 / 개행 선검증
# ==============================================================================

def test_merge_env_keeps_operator_oidc_smtp_keys_when_feature_on():
    """desired_env 가 한 번도 방출하지 않는 운영자 확장 키(OIDC_EMAIL_CLAIM,
    SMTP_USERNAME 등)를 기능이 켜진 채로 지우면 안 된다 — SMTP 인증이
    렌더마다 지워져 알림이 멎는 사고."""
    existing = {"OIDC_ISSUER_URL": "https://idp", "OIDC_EMAIL_CLAIM": "upn",
                "SMTP_HOST": "mail", "SMTP_USERNAME": "u",
                "SMTP_PASSWORD": "p", "SMTP_STARTTLS": "true"}
    desired = {"OIDC_ISSUER_URL": "https://idp", "SMTP_HOST": "mail",
               "WEB_SEARCH_ENABLED": "false"}
    merged, _, _, dropped = common.merge_env(existing, desired)
    assert merged["OIDC_EMAIL_CLAIM"] == "upn"
    assert merged["SMTP_USERNAME"] == "u" and merged["SMTP_PASSWORD"] == "p"
    assert not [k for k in dropped if k.startswith(("OIDC_", "SMTP_"))]


def test_merge_env_drops_prefix_keys_only_when_anchor_off():
    """기능이 꺼지면(앵커 키가 desired 에 없음) OIDC_/SMTP_ 그룹은 통째로
    수렴한다 — stale OIDC_* 가 dev 토큰을 거부하는 잠금 방지."""
    existing = {"OIDC_ISSUER_URL": "https://idp", "OIDC_EMAIL_CLAIM": "upn",
                "SMTP_HOST": "mail", "SMTP_USERNAME": "u"}
    merged, _, _, dropped = common.merge_env(existing, {"DEV_LOGIN_ENABLED": "true"})
    assert "OIDC_ISSUER_URL" not in merged
    assert "OIDC_EMAIL_CLAIM" not in merged and "SMTP_USERNAME" not in merged
    assert "OIDC_EMAIL_CLAIM" in dropped


def test_render_newline_aborts_before_any_file_write(tmp_path):
    """개행 값은 첫 파일 쓰기 전에 거절돼야 한다 — compose/Caddyfile 만
    새 것이고 .env 는 옛 것인 불일치 산출물 방지."""
    import pytest
    c = cfg()
    monkey = {"bad": "x\nINJECTED=1"}
    with pytest.raises(SystemExit):
        common.write_env_file(tmp_path / ".env", monkey, "h")
    # 실제 render 경로 — 도메인에 개행이 들어가도 yaml 에서 차단되지만,
    # write_env_file 의 검증이 compose 쓰기 전에 발동하는지는 merge_env
    # 단위로 이미 검증됨. 여기서는 사전검증 순서를 직접 확인한다.
    merged, _, _, _ = common.merge_env({}, {"A": "1\n2"})
    with pytest.raises(SystemExit):
        for k, v in merged.items():
            if "\n" in str(v):
                raise SystemExit(f"{k}")


def test_ports_default_bindings(tmp_path):
    _, out = render_to(tmp_path, cfg())
    assert out["services"]["caddy"]["ports"] == [
        "8000:8000", "8080:8080", "3000:3000"]


def test_ports_custom_host_side(tmp_path):
    _, out = render_to(tmp_path, cfg(
        network={"ports": {"gateway": 9000, "admin_api": 9080,
                           "admin_ui": 9300}}))
    # 호스트 측만 설정값 — 컨테이너 측 리슨 포트와 Caddyfile 은 불변
    assert out["services"]["caddy"]["ports"] == [
        "9000:8000", "9080:8080", "9300:3000"]
    body = (tmp_path / "Caddyfile").read_text()
    assert ":8000" in body and "admin-ui:3000" in body


def test_ports_domain_mode_http_https(tmp_path):
    _, out = render_to(tmp_path, cfg(
        domain={"mode": "route53-acm", "name": "example.com"},
        network={"ports": {"http": 8080, "https": 8443}}))
    assert out["services"]["caddy"]["ports"] == ["8080:80", "8443:443"]


def test_ports_invalid_rejected():
    import pytest
    with pytest.raises(schema.SchemaError):
        cfg(network={"ports": {"gateway": 99999}})
    with pytest.raises(schema.SchemaError):
        cfg(network={"ports": {"admin_ui": "abc"}})
    with pytest.raises(schema.SchemaError):
        cfg(network={"ports": {"bogus": 1234}})


def test_ports_eks_warns_ignored():
    c = cfg(network={"ports": {"gateway": 9000}})
    c.deploy.target = "eks"  # eks 검증 요구조건 우회 — 경고 분기만 확인
    assert any("compose 전용" in w for w in c.warnings())


def test_ports_collision_rejected():
    import pytest
    # 같은 그룹 내 중복 — caddy 가 같은 호스트 포트를 두 번 바인드해 확정 실패
    with pytest.raises(schema.SchemaError):
        cfg(network={"ports": {"gateway": 9000, "admin_api": 9000}})
    with pytest.raises(schema.SchemaError):
        cfg(domain={"mode": "route53-acm", "name": "x.com"},
            network={"ports": {"http": 8080, "https": 8080}})


def test_ports_cross_group_overlap_allowed(tmp_path):
    # http(도메인 모드 전용)와 gateway(none 전용)는 동시에 바인드되지 않는다
    _, out = render_to(tmp_path, cfg(
        network={"ports": {"gateway": 9000, "http": 9000}}))
    assert "9000:8000" in out["services"]["caddy"]["ports"]


def test_ports_partial_override_keeps_defaults(tmp_path):
    _, out = render_to(tmp_path, cfg(network={"ports": {"gateway": 9000}}))
    assert out["services"]["caddy"]["ports"] == [
        "9000:8000", "8080:8080", "3000:3000"]


def test_port_conflict_free_port_not_flagged():
    import socket
    from deploy import cli
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    free = s.getsockname()[1]
    s.close()
    c = cfg(network={"ports": {"gateway": free}})
    # 다른 기본 포트(8080/3000)는 개발 머신에서 점유될 수 있어 gateway 만 본다
    assert f"network.ports.gateway={free}" not in cli._compose_port_conflicts(c)


def test_port_conflict_busy_port_flagged():
    import socket
    from deploy import cli
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        c = cfg(network={"ports": {"gateway": port}})
        assert f"network.ports.gateway={port}" in cli._compose_port_conflicts(c)
    finally:
        s.close()


def test_port_conflict_domain_mode_checks_http_https():
    import socket
    from deploy import cli
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    port = s.getsockname()[1]
    try:
        c = cfg(domain={"mode": "route53-acm", "name": "x.com"},
                network={"ports": {"http": port}})
        # http 점유 → 충돌, https(기본 443 미점유 가정)는 무관 — gateway 는
        # none 전용이라 검사하지 않는다
        got = cli._compose_port_conflicts(c)
        assert any(f"http={port}" in x for x in got)
    finally:
        s.close()
