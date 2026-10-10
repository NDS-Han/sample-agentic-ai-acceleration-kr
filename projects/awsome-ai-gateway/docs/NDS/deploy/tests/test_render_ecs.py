# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""ecs renderer — gateway.yaml → tfvars 변환 규칙 테스트."""
from __future__ import annotations

import pytest

from deploy import schema
from deploy.render import ecs as ecs_render


def _cfg(**over) -> schema.GatewayConfig:
    doc = {
        "version": 1,
        "env": "test",
        "aws": {"region": "ap-northeast-2", "account_id": "111122223333"},
        "deploy": {"target": "ecs", "size_tier": "t1"},
        "network": {"mode": "public", "allowed_cidrs": ["10.0.0.0/8"]},
        "domain": {"mode": "none"},
        "features": {"notifications": {"provider": "mock"}},
        "images": {"tag": "v1"},
        "clients": {"models_profile": "global"},
    }
    doc.update(over)
    return schema.from_dict(doc)


def test_t1_preset_maps_to_serverless_and_single_cache():
    v = ecs_render.desired_tfvars(_cfg())
    assert v["db_mode"] == "serverless"
    assert v["serverless_min_acu"] == 1.0
    assert v["serverless_max_acu"] == 4.0
    assert v["cache_mode"] == "single"
    assert v["nat_ha"] is False
    assert v["db_safeguards"] is True


def test_t2_preset_maps_to_multi_az_and_replicated():
    cfg = _cfg(deploy={"target": "ecs", "size_tier": "t2"})
    v = ecs_render.desired_tfvars(cfg)
    assert v["serverless_max_acu"] == 16.0
    assert v["cache_mode"] == "replicated"
    assert v["nat_ha"] is True
    assert v["service_sizing"]["gateway-proxy"]["desired_count"] == 4


def test_t0_rejected_for_ecs():
    with pytest.raises(schema.SchemaError, match="size_tier"):
        _cfg(deploy={"target": "ecs", "size_tier": "t0"})


def test_private_network_sets_alb_internal():
    cfg = _cfg(network={"mode": "private"})
    assert ecs_render.desired_tfvars(cfg)["alb_internal"] is True


def test_domain_route53_needs_zone_id():
    with pytest.raises(schema.SchemaError, match="zone_id"):
        _cfg(domain={"mode": "route53-acm", "name": "example.com"})
    cfg = _cfg(domain={"mode": "route53-acm", "name": "example.com", "zone_id": "Z123"})
    v = ecs_render.desired_tfvars(cfg)
    assert v["domain_name"] == "example.com"
    assert v["hosted_zone_id"] == "Z123"


def test_cloudfront_temp_falls_back_with_note():
    cfg = _cfg(domain={"mode": "cloudfront-temp"})
    assert ecs_render.desired_tfvars(cfg)["domain_name"] == ""
    assert any("cloudfront-temp" in n for n in ecs_render.feature_notes(cfg))


def test_ses_notification_vars():
    cfg = _cfg(features={"notifications": {"provider": "ses", "ses_from": "a@b.c"}})
    v = ecs_render.desired_tfvars(cfg)
    assert v["email_sender_type"] == "ses"
    assert v["email_sender_address"] == "a@b.c"
    assert v["aws_ses_region"] == "ap-northeast-2"


def test_smtp_notification_vars():
    cfg = _cfg(features={"notifications": {
        "provider": "smtp", "smtp_host": "mail.corp", "smtp_from": "a@b.c"}})
    v = ecs_render.desired_tfvars(cfg)
    assert v["smtp_host"] == "mail.corp"
    assert v["smtp_port"] == 587


def test_global_model_profile_arns():
    v = ecs_render.desired_tfvars(_cfg())
    assert any("global.anthropic" in a for a in v["bedrock_allowed_model_arns"])


def test_regional_model_profile_arns():
    cfg = _cfg(clients={"models_profile": "regional"})
    v = ecs_render.desired_tfvars(cfg)
    assert not any("global.anthropic" in a for a in v["bedrock_allowed_model_arns"])
    assert any("ap-northeast-2" in a for a in v["bedrock_allowed_model_arns"])


def test_sizing_override():
    cfg = _cfg(deploy={"target": "ecs", "size_tier": "t1",
                       "sizing": {"gateway-proxy": {"cpu": 2048}}})
    v = ecs_render.desired_tfvars(cfg)
    assert v["service_sizing"]["gateway-proxy"]["cpu"] == 2048
    assert v["service_sizing"]["gateway-proxy"]["memory"] == 2048  # 나머지는 유지


def test_cost_recorder_stays_single():
    """스트림 consumer 고정 — 티어가 올라도 1개."""
    v = ecs_render.desired_tfvars(_cfg(deploy={"target": "ecs", "size_tier": "t2"}))
    assert v["service_sizing"]["cost-recorder-worker"]["desired_count"] == 1


def test_backend_hcl_infers_from_account_id():
    cfg = _cfg()
    hcl = ecs_render.render_backend_hcl(cfg)
    assert 'bucket         = "llm-gateway-tfstate-111122223333"' in hcl
    assert 'key            = "ecs/test/terraform.tfstate"' in hcl


def test_backend_hcl_placeholder_without_account():
    cfg = _cfg(aws={"region": "ap-northeast-2"})
    hcl = ecs_render.render_backend_hcl(cfg)
    assert "YOUR_TFSTATE_BUCKET" in hcl
    assert "placeholder" in hcl


def test_backend_hcl_explicit():
    cfg = _cfg(deploy={"target": "ecs", "size_tier": "t1",
                       "tfstate_bucket": "mybucket", "tfstate_table": "mytable"})
    hcl = ecs_render.render_backend_hcl(cfg)
    assert 'bucket         = "mybucket"' in hcl
    assert 'dynamodb_table = "mytable"' in hcl


def test_feature_notes_for_flags():
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "web_search": True, "bi_insight": True,
                         "pricing_lambda": True})
    notes = ecs_render.feature_notes(cfg)
    assert any("web_search" in n for n in notes)
    assert any("bi_insight" in n for n in notes)
    assert any("pricing_lambda" in n for n in notes)


def test_render_writes_files(tmp_path):
    res = ecs_render.render(_cfg(), tmp_path)
    names = {f.name for f in res["files"]}
    assert names == {"terraform.tfvars", "backend.hcl"}
    tfvars = (tmp_path / "terraform.tfvars").read_text()
    assert 'db_mode = "serverless"' in tfvars
    assert "generated by deploy" in tfvars


def test_extra_vars_merge(tmp_path):
    """apply 가 발견한 ALB DNS 주소가 tfvars 에 기록된다."""
    ecs_render.render(_cfg(), tmp_path, extra_vars={
        "admin_ui_nextauth_url": "http://lb-123.elb.amazonaws.com:3000",
        "gateway_external_url": "http://lb-123.elb.amazonaws.com:8000",
    })
    tfvars = (tmp_path / "terraform.tfvars").read_text()
    assert 'admin_ui_nextauth_url = "http://lb-123.elb.amazonaws.com:3000"' in tfvars


def test_derived_url_vars():
    from deploy import ecs_apply
    cfg = _cfg()
    out = {"alb_dns_name": "lb-1.elb.amazonaws.com"}
    v = ecs_apply._derived_url_vars(cfg, out)
    assert v["admin_ui_nextauth_url"] == "http://lb-1.elb.amazonaws.com:3000"
    # 도메인 있으면 발견값 없음 (도메인에서 파생)
    cfg2 = _cfg(domain={"mode": "route53-acm", "name": "ex.com", "zone_id": "Z1"})
    assert ecs_apply._derived_url_vars(cfg2, out) == {}


def test_registry_host_gets_project_prefix_appended():
    # images.registry 의 정본 의미는 "레지스트리 호스트" (eks chart 의
    # global.imageRegistry 와 동일 — 차트가 뒤에 llm-gateway/<svc> 를 붙인다).
    # ecs 모듈은 image_registry/<svc> 로 조립하므로 render 가 /llm-gateway 를
    # 붙여 실제 repo(host/llm-gateway/<svc>)와 일치시킨다.
    v = ecs_render.desired_tfvars(_cfg(
        images={"tag": "v1", "registry": "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com"}))
    assert v["image_registry"] == "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/llm-gateway"


def test_registry_with_project_prefix_passes_through():
    # 이미 path 가 붙은 경우(비표준 repo prefix)는 사용자 의도로 간주해 그대로 둔다.
    v = ecs_render.desired_tfvars(_cfg(
        images={"tag": "v1", "registry": "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/custom"}))
    assert v["image_registry"] == "111122223333.dkr.ecr.ap-northeast-2.amazonaws.com/custom"


def test_empty_registry_leaves_module_to_create_repos():
    v = ecs_render.desired_tfvars(_cfg())
    assert "image_registry" not in v
