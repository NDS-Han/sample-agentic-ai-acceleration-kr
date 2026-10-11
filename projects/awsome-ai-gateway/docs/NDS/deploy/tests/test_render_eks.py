# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks 렌더러 테스트 — 오버레이가 관리 키만 담고 env values 와 레이어링."""
from __future__ import annotations

import re

import yaml
import pytest

from .. import schema
from ..render import eks as eks_render

BASE = {
    "version": 1,
    "env": "dev",
    "aws": {"region": "ap-south-1"},
    "deploy": {"target": "eks", "size_tier": "t3"},
    "network": {"mode": "public", "allowed_cidrs": ["10.0.0.0/8"]},
    "domain": {"mode": "route53-acm", "name": "gw.example.com"},
    "features": {"notifications": {"provider": "ses", "ses_from": "ops@x.io"},
                 "web_search": True},
    "images": {"tag": "1.0.170"},
    "clients": {"models_profile": "global"},
    "oidc": {"issuer_url": "https://cognito-idp.ap-south-1.amazonaws.com/ap-x",
             "client_id": "cid",
             "authorize_url": "https://a/oauth2/authorize",
             "token_url": "https://a/oauth2/token"},
}


def _render(tmp_path, doc=BASE):
    cfg = schema.from_dict(doc)
    out = tmp_path / "gen" / cfg.env / "eks"
    res = eks_render.render(cfg, out)
    return cfg, out, res


def test_overlay_owns_managed_keys(tmp_path):
    _, out, _ = _render(tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["aws"]["region"] == "ap-south-1"
    assert v["aws"]["allowedStsRegions"] == ["ap-south-1"]
    for svc in eks_render.TAGGED_SERVICES:
        assert v[svc]["image"]["tag"] == "1.0.170"
    assert v["gatewayProxy"]["env"]["WEB_SEARCH_ENABLED"] == "true"
    assert v["notificationWorker"]["email"]["provider"] == "ses"
    assert v["adminApi"]["oidc"]["issuerUrl"].startswith("https://cognito-idp")
    # audience 미설정이면 aud 검증이 꺼진다 — 명시 audience 없으면 client_id 폴백.
    assert v["adminApi"]["oidc"]["audience"] == "cid"
    assert v["adminUi"]["env"]["DEV_LOGIN_ENABLED"] == "false"
    ann = v["ingress"]["annotations"]
    assert ann["alb.ingress.kubernetes.io/inbound-cidrs"] == "10.0.0.0/8"
    assert v["ingress"]["gateway"]["host"] == "gateway.gw.example.com"


def test_per_service_tags_override_default_and_pin_migration(tmp_path):
    # US-19 멀티태그 릴리스 — tags 가 tag 기본값 위에 개별 서비스를 덮고
    # migration(TAGGED_SERVICES 밖)도 핀할 수 있어야 한다.
    doc = dict(BASE, images={"tag": "1.0.170",
                             "tags": {"gatewayProxy": "1.0.87-us19",
                                      "migration": "1.0.54-us19"}})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["gatewayProxy"]["image"]["tag"] == "1.0.87-us19"
    assert v["adminApi"]["image"]["tag"] == "1.0.170"          # 기본값 유지
    assert v["migration"]["image"]["tag"] == "1.0.54-us19"


def test_tags_only_render_without_default_tag(tmp_path):
    doc = dict(BASE, images={"tags": {s: "1.0.87-us19" for s in
                                      eks_render.TAGGED_SERVICES}})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    for svc in eks_render.TAGGED_SERVICES:
        assert v[svc]["image"]["tag"] == "1.0.87-us19"
    assert "tag" not in v["migration"].get("image", {})       # env overlay 에 위임


def test_overlay_does_not_leak_dynamic_values(tmp_path):
    _, out, _ = _render(tmp_path)
    text = (out / "values.yaml").read_text()
    # IRSA ARN·DB/Redis endpoint·Firehose 는 apply 의 --set 영역 — 오버레이에 없어야 함
    for forbidden in ("role-arn", "database.external", "FIREHOSE_STREAM_NAME",
                      "elasticache", "secretName"):
        assert forbidden not in text, forbidden


def test_deploy_meta_layers_env_values_first(tmp_path):
    cfg, out, _ = _render(tmp_path)
    meta = yaml.safe_load((out / "deploy.yaml").read_text())
    assert meta["release"] == "llm-gateway"
    assert meta["namespace"] == "llm-gateway"
    assert meta["env_dir"] == "deployment/terraform/environments/llm-gateway-dev"
    # env overlay(values-eks-fargate-dev[.local].yaml 이 실제 존재)가 먼저, 우리 것이 마지막
    layers = meta["values_layers"]
    assert re.search(r"values-eks-fargate-dev(\.local)?\.yaml$", layers[0])
    assert layers[-1].endswith("eks/values.yaml")


def test_domain_none_keeps_existing_hosts(tmp_path):
    doc = dict(BASE)
    doc["domain"] = {"mode": "none"}
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    # domain.name 없으면 host 를 건드리지 않는다 — 기존 values 승계
    assert "gateway" not in (v.get("ingress") or {})
