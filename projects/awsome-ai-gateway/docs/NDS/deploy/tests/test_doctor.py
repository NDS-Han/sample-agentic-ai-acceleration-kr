# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""doctor 순수 함수 — per-service 태그 기대값 계산."""
from deploy import doctor, schema


def _cfg(images):
    return schema.from_dict({
        "version": 1, "env": "dev",
        "deploy": {"target": "eks", "size_tier": "t3"},
        "images": images})


def test_expected_tag_single_tag_all_services():
    cfg = _cfg({"tag": "abc123"})
    assert doctor._expected_tag(cfg, "gateway-proxy") == "abc123"
    assert doctor._expected_tag(cfg, "admin-api") == "abc123"


def test_expected_tag_per_service_pin_overrides():
    cfg = _cfg({"tag": "abc123", "tags": {"adminApi": "other-tag"}})
    assert doctor._expected_tag(cfg, "admin-api") == "other-tag"
    assert doctor._expected_tag(cfg, "gateway-proxy") == "abc123"


def test_expected_tag_tags_only_no_default():
    cfg = _cfg({"tags": {"gatewayProxy": "pin-a"}})
    assert doctor._expected_tag(cfg, "gateway-proxy") == "pin-a"
    # 핀 없는 서비스는 기대값 없음 → drift 검사 생략 (env overlay 태그)
    assert doctor._expected_tag(cfg, "admin-ui") == ""


def test_expected_tag_migration_not_a_deployment():
    # migration 은 EKS_DEPLOYMENTS 에 없고 kebab 매핑 대상도 아니다 —
    # images.tags.migration 핀이 서비스 기대값으로 새어나가면 안 된다
    cfg = _cfg({"tag": "abc", "tags": {"migration": "mig-tag"}})
    assert doctor._expected_tag(cfg, "gateway-proxy") == "abc"
