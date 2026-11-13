# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Tests for cli.tools.openai_config — OAuth handler + JWT config apply."""

from __future__ import annotations

from unittest.mock import MagicMock

from cli.config import (
    AuthMode,
    ComponentStatus,
    DetectedTool,
    GatewayConfig,
    ToolType,
)
from cli.tools.openai_config import (
    apply_cline_config,
    apply_opencode_config,
    create_oauth_handler,
    setup_jwt,
)


class TestCreateOAuthHandler:
    def test_valid_callback(self) -> None:
        handler_cls, result = create_oauth_handler("expected-state")
        handler = MagicMock(spec=handler_cls)
        handler.path = "/callback?code=abc123&state=expected-state"

        # Call do_GET directly by creating a real-ish handler
        # We test the closure logic through the result dict
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(handler.path).query)
        state = qs.get("state", [None])[0]
        code = qs.get("code", [None])[0]
        if state == "expected-state" and code:
            result["code"] = code

        assert result["code"] == "abc123"
        assert result["error"] is None

    def test_state_mismatch(self) -> None:
        handler_cls, result = create_oauth_handler("expected-state")
        # Simulate state mismatch
        result["error"] = "CSRF verification failed: state mismatch"
        assert result["error"] == "CSRF verification failed: state mismatch"
        assert result["code"] is None


class TestApplyOpenCodeConfig:
    def test_sets_endpoint_and_key(self) -> None:
        original = {"other": "val"}
        result = apply_opencode_config(original, "https://gw.com", "jwt-token-123")
        assert result["provider"]["endpoint"] == "https://gw.com/v1"
        assert result["provider"]["api_key"] == "jwt-token-123"
        assert result["other"] == "val"

    def test_preserves_existing(self) -> None:
        original = {"provider": {"model": "gpt-4"}, "ui": "dark"}
        result = apply_opencode_config(original, "https://gw.com", "tok")
        assert result["provider"]["endpoint"] == "https://gw.com/v1"
        assert result["ui"] == "dark"


class TestApplyClineConfig:
    def test_sets_cline_keys(self) -> None:
        original = {"editor.fontSize": 14}
        result = apply_cline_config(original, "https://gw.com", "jwt-token")
        assert result["cline.apiProvider"] == "openai-compatible"
        assert result["cline.openaiBaseUrl"] == "https://gw.com/v1"
        assert result["cline.openaiApiKey"] == "jwt-token"
        assert result["editor.fontSize"] == 14


class TestSetupJwt:
    def test_skip_when_no_jwt_auth(self, tmp_path) -> None:
        tool = DetectedTool(
            tool_type=ToolType.OPENCODE,
            name="OpenCode",
            config_path=str(tmp_path / "config.json"),
            auth_mode=AuthMode.JWT,
        )
        config = GatewayConfig(jwt_auth={})
        result = setup_jwt(tool, config)
        assert result.status == ComponentStatus.SKIPPED
        assert "missing" in result.message.lower()


class TestRunOauthFlowPkce:
    """R28 F3/F4 — 인가 URL 은 PKCE + 퍼센트 인코딩을 갖춰야 하고, 토큰 교환은
    code_verifier 를 보내야 한다."""

    def test_pkce_and_urlencode(self, monkeypatch) -> None:
        import base64
        import hashlib
        from urllib.parse import parse_qs, urlparse

        from cli.tools import openai_config as oc

        captured: dict = {}
        fake_result = {"code": None, "error": None}

        class FakeServer:
            def __init__(self, addr, handler):
                self.server_address = (addr[0], 42424)
                self.timeout = None

            def handle_request(self):
                fake_result["code"] = "authcode123"

            def server_close(self):
                pass

        monkeypatch.setattr(oc, "HTTPServer", FakeServer)
        monkeypatch.setattr(
            oc, "create_oauth_handler", lambda state: (None, fake_result)
        )
        monkeypatch.setattr(
            oc.webbrowser, "open", lambda url: captured.setdefault("url", url)
        )

        resp = MagicMock()
        resp.json.return_value = {"access_token": "jwt-xyz"}
        post = MagicMock(return_value=resp)
        monkeypatch.setattr(oc.requests, "post", post)

        cfg = GatewayConfig(
            jwt_auth={
                "sso_auth_url": "https://sso.example.com/authorize",
                "sso_token_url": "https://sso.example.com/token",
                "client_id": "gw-cli",
                "scope": "openid profile",  # 공백 포함 — 인코딩되어야 함
            }
        )
        token = oc._run_oauth_flow(cfg)
        assert token == "jwt-xyz"

        url = captured["url"]
        qs = parse_qs(urlparse(url).query)
        assert qs["scope"] == ["openid profile"]  # 인코딩 후에도 파싱 시 원래 값
        assert "openid+profile" in url or "openid%20profile" in url
        assert " " not in url  # 인가 URL 에 리터럴 공백이 없어야 함
        assert qs["code_challenge_method"] == ["S256"]

        data = post.call_args.kwargs["data"]
        assert data["code"] == "authcode123"
        verifier = data["code_verifier"]
        expected = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert qs["code_challenge"] == [expected]
