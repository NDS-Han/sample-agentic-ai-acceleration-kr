# Copyright 2026 © Amazon.com and Affiliates.
"""init wizard OIDC 분기 — 대상별로 IdP 질문이 억제/활성화된다.

eks/ecs 는 배포가 Cognito 를 만들어 OIDC 엔드포인트를 자동 관리하므로
issuer/client/URL 을 묻지 않거나(eks-auto) 아예 읽지 않는다(ecs). 외부
IdP 를 쓰는 경우(eks-ext, compose)에만 URL 질문이 떠야 한다."""
from __future__ import annotations

import types

import pytest

from .. import cli


class FakeQ:
    """questionary 네임스페이스 대역 — prompt 부분문자열 → 스크립트된 답."""

    def __init__(self, answers: dict):
        self._a = answers

    def _mk(self, prompt: str, fallback):
        for k, v in self._a.items():
            if k in prompt:
                return types.SimpleNamespace(ask=lambda: v)
        return types.SimpleNamespace(ask=lambda: fallback)

    def text(self, prompt, **kw):
        return self._mk(prompt, kw.get("default", ""))

    def confirm(self, prompt, **kw):
        return self._mk(prompt, kw.get("default", False))

    def select(self, prompt, choices=None, **kw):
        fb = kw.get("default")
        if fb is None and choices:
            c0 = choices[0]
            fb = c0.value if hasattr(c0, "value") else c0
        return self._mk(prompt, fb)

    def checkbox(self, prompt, choices=None, **kw):
        return self._mk(prompt, [])

    def password(self, prompt, **kw):
        return self._mk(prompt, "")


BASE = {
    "환경 이름": "t9",
    "AWS 리전": "ap-south-1",
    "알림 provider": "mock",
    "외부 이미지 registry": "",
    "이미지 태그": "",
    "Terraform state S3": "",
    "Terraform lock": "",
    "접근 허용 CIDR": "",
    "admin 콘솔": False,
    "DB/캐시 크기": False,
}

EXT_ANSWERS = {
    "OIDC issuer URL": "https://idp.example.com",
    "OIDC client id": "cid-1",
    "authorize URL": "https://idp.example.com/authorize",
    "token URL": "https://idp.example.com/token",
    "VK 수명": "",
    "그룹 prefix": "",
    "ADMIN 역할 부여 그룹": "",
    "ADMIN 이메일": "",
}


def _doc(monkeypatch, answers: dict, existing: dict | None = None) -> dict:
    monkeypatch.setattr(cli, "_questionary", lambda: FakeQ(answers))
    monkeypatch.setattr(cli, "_aws_account_id",
                        lambda *a, **k: "123456789012")
    return cli._collect_doc(existing)


class TestEksOidcSelect:
    def test_auto_leaves_oidc_empty(self, monkeypatch):
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "route53-acm",
                                 "베이스 도메인": "example.com",
                                 "Route53 hosted zone": "",
                                 "OIDC 로그인 방식": "auto"})
        assert "oidc" not in doc

    def test_auto_strips_idp_keeps_aux(self, monkeypatch):
        existing = {"oidc": {"issuer_url": "https://idp", "client_id": "c",
                             "authorize_url": "a", "token_url": "t",
                             "admin_groups": ["G"], "vk_ttl_hours": 8}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "none",
                                 "OIDC 로그인 방식": "auto"}, existing)
        assert doc["oidc"] == {"admin_groups": ["G"], "vk_ttl_hours": 8}

    def test_ext_asks_idp_urls(self, monkeypatch):
        doc = _doc(monkeypatch, {**BASE, **EXT_ANSWERS,
                                 "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "none",
                                 "OIDC 로그인 방식": "ext"})
        assert doc["oidc"]["issuer_url"] == "https://idp.example.com"
        assert doc["oidc"]["client_id"] == "cid-1"

    def test_none_declined_removal_restores(self, monkeypatch):
        existing = {"oidc": {"issuer_url": "https://idp", "client_id": "c"}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "none",
                                 "OIDC 로그인 방식": "none",
                                 "제거합니까": False}, existing)
        assert doc["oidc"] == existing["oidc"]  # 제거 거부 → 기존 유지

    def test_none_confirmed_removes(self, monkeypatch):
        existing = {"oidc": {"issuer_url": "https://idp", "client_id": "c"}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "none",
                                 "OIDC 로그인 방식": "none",
                                 "제거합니까": True}, existing)
        assert "oidc" not in doc


class TestEcsOidc:
    def test_never_asks_and_strips_idp(self, monkeypatch):
        existing = {"oidc": {"issuer_url": "https://idp", "client_id": "c",
                             "group_prefix": "X_", "vk_ttl_hours": 4}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "ecs",
                                 "규모 티어": "t2",
                                 "HTTPS 도메인": "none"}, existing)
        # IdP 키는 제거, 보조 필드는 보존 — 질문 자체가 없었음
        assert doc["oidc"] == {"group_prefix": "X_", "vk_ttl_hours": 4}

    def test_aux_only_survives(self, monkeypatch):
        existing = {"oidc": {"admin_emails": ["a@b.c"]}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "ecs",
                                 "규모 티어": "t2",
                                 "HTTPS 도메인": "none"}, existing)
        assert doc["oidc"] == {"admin_emails": ["a@b.c"]}


class TestComposeOidc:
    def test_no_external_idp_leaves_empty(self, monkeypatch):
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "compose",
                                 "규모 티어": "t0", "HTTPS 도메인": "none",
                                 "외부 OIDC IdP": False})
        assert "oidc" not in doc

    def test_external_idp_asks_urls(self, monkeypatch):
        doc = _doc(monkeypatch, {**BASE, **EXT_ANSWERS,
                                 "배포 대상": "compose", "규모 티어": "t0",
                                 "HTTPS 도메인": "none",
                                 "외부 OIDC IdP": True})
        assert doc["oidc"]["issuer_url"] == "https://idp.example.com"


class TestDevLoginQuestion:
    """로그인 경로가 비는 경우만 dev_login 을 묻고, 인증 경로가 생기면
    이전 true 를 명시적으로 끈다 — 무인증 ADMIN 경로의 침묵 잔존 방지."""

    def test_compose_no_idp_asks(self, monkeypatch):
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "compose",
                                 "규모 티어": "t0", "HTTPS 도메인": "none",
                                 "외부 OIDC IdP": False,
                                 "dev_login 을 켭니까": True})
        assert doc["features"]["dev_login"] is True

    def test_compose_blank_issuer_still_asks(self, monkeypatch):
        # "IdP 있음" 으로 답하고 issuer 를 비우면 인증 경로가 없다 — 물어야 함
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "compose",
                                 "규모 티어": "t0", "HTTPS 도메인": "none",
                                 "외부 OIDC IdP": True,
                                 "OIDC issuer URL": "",
                                 "dev_login 을 켭니까": True})
        assert doc["features"]["dev_login"] is True

    def test_compose_no_overwrites_prev_true(self, monkeypatch):
        existing = {"features": {"dev_login": True}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "compose",
                                 "규모 티어": "t0", "HTTPS 도메인": "none",
                                 "외부 OIDC IdP": False,
                                 "dev_login 을 켭니까": False}, existing)
        assert doc["features"]["dev_login"] is False

    def test_eks_none_asks(self, monkeypatch):
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "none",
                                 "OIDC 로그인 방식": "none",
                                 "dev_login 을 켭니까": True})
        assert doc["features"]["dev_login"] is True

    def test_eks_auto_domain_strips_stale_true(self, monkeypatch):
        # Cognito 자동 배선 경로인데 이전 dev_login:true 잔존 → 명시 해제
        existing = {"features": {"dev_login": True}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "route53-acm",
                                 "베이스 도메인": "example.com",
                                 "Route53 hosted zone": "",
                                 "OIDC 로그인 방식": "auto"}, existing)
        assert doc["features"]["dev_login"] is False

    def test_eks_ext_preserves_dev_login(self, monkeypatch):
        # 외부 IdP 경로는 yaml 의 issuer 로 render 가 dev_login 을 게이트한다 —
        # 보존해도 DEV_LOGIN_ENABLED=false 라 안전(SSO+dev fallback 의도 허용)
        existing = {"features": {"dev_login": True}}
        doc = _doc(monkeypatch, {**BASE, **EXT_ANSWERS,
                                 "배포 대상": "eks", "규모 티어": "t3",
                                 "HTTPS 도메인": "none",
                                 "OIDC 로그인 방식": "ext"}, existing)
        assert doc["features"]["dev_login"] is True

    def test_ecs_never_asks_preserves(self, monkeypatch):
        # ecs 는 Cognito 자동 — 질문 없이 이전 값 보존 대신 해제(경로 있음)
        existing = {"features": {"dev_login": True}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "ecs",
                                 "규모 티어": "t2",
                                 "HTTPS 도메인": "none"}, existing)
        assert doc["features"]["dev_login"] is False

    def test_configure_preserves_network_ports(self, monkeypatch):
        existing = {"network": {"ports": {"gateway": 9000}}}
        doc = _doc(monkeypatch, {**BASE, "배포 대상": "compose",
                                 "규모 티어": "t0", "HTTPS 도메인": "none",
                                 "외부 OIDC IdP": False,
                                 "dev_login 을 켭니까": False}, existing)
        assert doc["network"]["ports"] == {"gateway": 9000}
