# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
import pytest

from deploy import schema


def minimal(**over):
    doc = {"version": 1, "env": "acme-test",
           "deploy": {"target": "compose", "size_tier": "t0"}}
    doc.update(over)
    return doc


def test_minimal_valid():
    cfg = schema.from_dict(minimal())
    assert cfg.env == "acme-test"
    assert cfg.deploy.target == "compose"
    assert cfg.oidc.enabled is False


def test_env_required():
    with pytest.raises(schema.SchemaError, match="env"):
        schema.from_dict(minimal(env=""))


def test_env_format():
    with pytest.raises(schema.SchemaError):
        schema.from_dict(minimal(env="Bad_Env!"))


def test_compose_rejects_non_t0():
    with pytest.raises(schema.SchemaError, match="t0"):
        schema.from_dict(minimal(deploy={"target": "compose", "size_tier": "t1"}))


def test_ecs_requires_image_tag():
    with pytest.raises(schema.SchemaError, match="images.tag"):
        schema.from_dict(minimal(
            deploy={"target": "ecs", "size_tier": "t1"},
            images={"registry": "r.example.com"}))


def test_eks_requires_registry():
    with pytest.raises(schema.SchemaError, match="registry"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tag": "abc123"}))


def test_domain_route53_requires_name():
    with pytest.raises(schema.SchemaError, match="domain.name"):
        schema.from_dict(minimal(domain={"mode": "route53-acm"}))


def test_oidc_partial_config_rejected():
    with pytest.raises(schema.SchemaError, match="client_id|authorize_url"):
        schema.from_dict(minimal(oidc={"issuer_url": "https://idp.example.com"}))


def test_oidc_full_valid():
    cfg = schema.from_dict(minimal(oidc={
        "issuer_url": "https://idp.example.com",
        "client_id": "abc",
        "authorize_url": "https://idp.example.com/oauth2/authorize",
        "token_url": "https://idp.example.com/oauth2/token",
    }))
    assert cfg.oidc.enabled


def test_bad_version():
    with pytest.raises(schema.SchemaError, match="version"):
        schema.from_dict(minimal(version=99))


def test_ses_requires_from():
    with pytest.raises(schema.SchemaError, match="ses_from"):
        schema.from_dict(minimal(features={"notifications": {"provider": "ses"}}))


def test_warnings_public_no_cidrs():
    cfg = schema.from_dict(minimal())
    assert any("allowed_cidrs" in w for w in cfg.warnings())


def test_warnings_no_cowork_without_tls():
    cfg = schema.from_dict(minimal())
    assert any("Cowork" in w for w in cfg.warnings())


def test_models_profile_default_global():
    cfg = schema.from_dict(minimal())
    assert cfg.clients.models_profile == "global"
