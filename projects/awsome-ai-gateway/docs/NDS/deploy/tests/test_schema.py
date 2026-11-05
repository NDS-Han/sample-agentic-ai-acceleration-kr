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


def test_eks_registry_optional_but_warns():
    # eks 는 apply 가 <account>.dkr.ecr.<region> 으로 추론 — 에러가 아니라 경고
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "abc123"}))
    assert any("registry" in w for w in cfg.warnings())


def test_eks_requires_tag():
    with pytest.raises(schema.SchemaError, match="tag"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={}))


def test_eks_tags_satisfy_tag_requirement():
    # US-19 같은 멀티태그 릴리스 — tag 없이 tags 만으로도 eks 검증을 통과해야 한다
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tags": {"gatewayProxy": "1.0.87-us19", "adminApi": "1.0.71-us19"}}))
    assert cfg.images.tags["gatewayProxy"] == "1.0.87-us19"


def test_eks_tags_reject_unknown_service_key():
    with pytest.raises(schema.SchemaError, match="images.tags"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tag": "x", "tags": {"bogusSvc": "1.0.0"}}))


def test_tags_rejected_on_compose():
    with pytest.raises(schema.SchemaError, match="eks"):
        schema.from_dict(minimal(
            images={"tags": {"gatewayProxy": "1.0.0"}}))


def test_eks_tag_without_migration_pin_warns():
    # 단일 tag 는 migration 이미지를 건드리지 않는다 — 운영자가 명시해야 인지 가능
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "1.0.87-us19"}))
    assert any("migration" in w for w in cfg.warnings())


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


def test_cidr_validation():
    with pytest.raises(schema.SchemaError, match="bogus"):
        schema.from_dict(minimal(
            network={"allowed_cidrs": ["bogus", "10.0.0.0"]}))


def test_cidr_string_wrapped_not_split():
    cfg = schema.from_dict(minimal(
        network={"allowed_cidrs": "1.2.3.4/32"}))  # 문자열 → 글자분해 아닌 단일 항목
    assert cfg.network.allowed_cidrs == ["1.2.3.4/32"]


def test_smtp_port_non_numeric_is_schema_error_not_valueerror():
    with pytest.raises(schema.SchemaError, match="smtp_port"):
        schema.from_dict(minimal(features={
            "notifications": {"provider": "smtp",
                              "smtp_host": "h", "smtp_port": "abc"}}))


def test_cloudfront_temp_only_eks():
    with pytest.raises(schema.SchemaError, match="cloudfront-temp"):
        schema.from_dict(minimal(domain={"mode": "cloudfront-temp", "name": "x.com"}))


# ── strict parsing — 오타 키/따옴표 bool 이 조용히 기본값이 되는 걸 막는다 ──

def test_unknown_top_level_key_rejected():
    with pytest.raises(schema.SchemaError, match="알 수 없는 키"):
        schema.from_dict(minimal(iamges={"tag": "x"}))   # images 오타


def test_unknown_section_key_rejected():
    with pytest.raises(schema.SchemaError, match="network.allowd_cidrs"):
        schema.from_dict(minimal(network={"allowd_cidrs": ["1.2.3.0/24"]}))


def test_unknown_oidc_key_rejected():
    with pytest.raises(schema.SchemaError, match="oidc"):
        schema.from_dict(minimal(oidc={
            "issuer_url": "https://idp.example.com",
            "client_id": "c", "authorize_url": "https://a", "token_url": "https://t",
            "client_secret_typo": "s"}))


def test_string_bool_feature_flag_rejected():
    # yaml 의 인용된 "false" 는 문자열 — bool() 이면 True 로 켜져버리므로 오류여야 한다
    with pytest.raises(schema.SchemaError, match="web_search"):
        schema.from_dict(minimal(features={"web_search": "false"}))


def test_int_bool_feature_flag_allowed():
    cfg = schema.from_dict(minimal(features={"web_search": 0}))
    assert cfg.features.web_search is False


def test_open_cidr_warns():
    # 캡처된 dev 배포에 실제로 있던 조합 — 빈 리스트와 같은 효과인데 경고가 없었다
    cfg = schema.from_dict(minimal(
        network={"mode": "public", "allowed_cidrs": ["65.2.11.169/32", "0.0.0.0/0"]}))
    assert any("0.0.0.0/0" in w for w in cfg.warnings())


def test_narrow_cidr_no_open_warning():
    cfg = schema.from_dict(minimal(
        network={"mode": "public", "allowed_cidrs": ["65.2.11.169/32"]}))
    assert not any("0.0.0.0/0" in w for w in cfg.warnings())


def test_eks_no_oidc_warns_dev_login():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "abc"}))
    assert any("DEV_LOGIN" in w for w in cfg.warnings())


def test_ecs_no_lock_table_warns():
    cfg = schema.from_dict(minimal(
        deploy={"target": "ecs", "size_tier": "t1"},
        images={"tag": "abc"}))
    assert any("tfstate_table" in w for w in cfg.warnings())


def test_sizing_unknown_service_rejected():
    with pytest.raises(schema.SchemaError, match="deploy.sizing"):
        schema.from_dict(minimal(
            deploy={"target": "ecs", "size_tier": "t1",
                    "sizing": {"gateway-prox": {"cpu": 2048}}},
            images={"tag": "abc"}))


def test_sizing_on_eks_rejected():
    with pytest.raises(schema.SchemaError, match="ecs 경로"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3",
                    "sizing": {"gateway-proxy": {"cpu": 2048}}},
            images={"tag": "abc"}))


def test_sizing_valid_on_ecs():
    cfg = schema.from_dict(minimal(
        deploy={"target": "ecs", "size_tier": "t1",
                "sizing": {"gateway-proxy": {"cpu": 2048}}},
        images={"tag": "abc"}))
    assert cfg.deploy.sizing["gateway-proxy"]["cpu"] == 2048


def test_compose_half_image_pair_warns():
    """registry 만/tag 만 선언된 compose 는 한쪽이 침묵 무시된다 — 경고 필요."""
    for images in ({"registry": "r.example.com"}, {"tag": "1.0.0"}):
        cfg = schema.from_dict(minimal(images=images))
        assert any("사전 빌드 이미지" in w for w in cfg.warnings()), images


def test_compose_full_image_pair_no_warn():
    cfg = schema.from_dict(minimal(
        images={"registry": "r.example.com", "tag": "1.0.0"}))
    assert not any("사전 빌드 이미지" in w for w in cfg.warnings())


def test_allowed_roles_scalar_is_wrapped():
    """R24-N9r2: 스칼라 문자열을 문자 단위로 쪼개지 않고 한 요소 리스트로."""
    cfg = schema.from_dict(minimal(aws={"allowed_roles": "DevRole"}))
    assert cfg.aws.allowed_roles == ["DevRole"]


def test_allowed_roles_list_unchanged():
    cfg = schema.from_dict(minimal(aws={"allowed_roles": ["A", "B"]}))
    assert cfg.aws.allowed_roles == ["A", "B"]


def test_compose_private_mode_warns_not_applied():
    """compose 렌더러는 network.mode 를 읽지 않는다 — private 선언이 침묵 무시돼
    호스트 포트가 그대로 열리므로 경고해야 한다."""
    cfg = schema.from_dict({
        "version": 1, "env": "dev",
        "deploy": {"target": "compose", "size_tier": "t0"},
        "network": {"mode": "private"},
    })
    assert any("private" in w and "compose" in w for w in cfg.warnings())


def test_observability_warns_on_non_compose():
    """observability 스택은 compose 전용 — ecs/eks 선언이 침묵 무시되면 안 된다."""
    cfg = schema.from_dict({
        "version": 1, "env": "dev",
        "deploy": {"target": "ecs", "size_tier": "t1"},
        "aws": {"region": "us-west-2", "account_id": "123456789012"},
        "images": {"registry": "r.example.com", "tag": "1.0.0"},
        "features": {"observability": True},
    })
    assert any("observability" in w and "compose" in w for w in cfg.warnings())


def test_models_profile_regional_warns_on_eks():
    """eks는 env tfvars/chart values가 모델 ARN을 소유 — regional 선언 무시 경고."""
    cfg = schema.from_dict({
        "version": 1, "env": "dev",
        "deploy": {"target": "eks", "size_tier": "t2"},
        "aws": {"region": "us-west-2", "account_id": "123456789012"},
        "images": {"registry": "r.example.com", "tag": "1.0.0"},
        "clients": {"models_profile": "regional"},
    })
    assert any("models_profile" in w and "eks" in w for w in cfg.warnings())


def test_models_profile_global_no_warn_on_eks():
    cfg = schema.from_dict({
        "version": 1, "env": "dev",
        "deploy": {"target": "eks", "size_tier": "t2"},
        "aws": {"region": "us-west-2", "account_id": "123456789012"},
        "images": {"registry": "r.example.com", "tag": "1.0.0"},
        "clients": {"models_profile": "global"},
    })
    assert not any("models_profile" in w for w in cfg.warnings())


def test_dev_login_warns_open_admin_path():
    cfg = schema.from_dict(minimal(features={"dev_login": True}))
    assert any("dev_login" in w for w in cfg.warnings())


def test_domain_hosts_parsed():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        domain={"mode": "route53-acm", "name": "example.com",
                "hosts": {"gateway": "gateway-dev.example.com",
                          "admin_ui": "admin-dev.example.com"}}))
    assert cfg.domain.hosts["gateway"] == "gateway-dev.example.com"
    assert cfg.domain.hosts["admin_ui"] == "admin-dev.example.com"


def test_domain_hosts_rejects_unknown_key():
    with pytest.raises(schema.SchemaError):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tag": "x"},
            domain={"mode": "route53-acm", "name": "example.com",
                    "hosts": {"bogus": "x.example.com"}}))


def test_domain_hosts_without_name_warns():
    # name 없는 hosts 는 render 가 무시한다 — 침묵 무시 대신 경고.
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        domain={"hosts": {"gateway": "gateway.example.com"}}))
    assert any("domain.hosts" in w for w in cfg.warnings())


def test_infra_mantle_regions_parsed():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        infra={"mantle_regions": ["us-east-1"]}))
    assert cfg.infra.mantle_regions == ["us-east-1"]


def test_infra_mantle_regions_scalar_wrapped():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        infra={"mantle_regions": "us-east-1"}))
    assert cfg.infra.mantle_regions == ["us-east-1"]


def test_domain_hosts_empty_mapping_ok():
    # {} 는 오버라이드 없음 — mapping 이 아니다 오류가 나면 안 된다.
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        domain={"name": "example.com", "hosts": {}}))
    assert cfg.domain.hosts == {}


def test_domain_hosts_null_value_rejected():
    # None 을 str() 하면 "None" 리터럴 호스트가 렌더된다 — 파싱에서 거절.
    with pytest.raises(schema.SchemaError):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tag": "x"},
            domain={"name": "example.com",
                    "hosts": {"gateway": None}}))


def test_domain_hosts_rejects_non_hostname():
    for bad in ("UPPER.example.com", "https://gw.example.com",
                "gw.example.com/path", "", "gw_example.com"):
        with pytest.raises(schema.SchemaError):
            schema.from_dict(minimal(
                deploy={"target": "eks", "size_tier": "t3"},
                images={"tag": "x"},
                domain={"name": "example.com",
                        "hosts": {"gateway": bad}}))


def test_domain_hosts_partial_warns_derived_fallback():
    # 일부만 선언하면 나머지는 파생 호스트로 바뀐다 — 침묵 fallback 방지 경고.
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        domain={"name": "example.com",
                "hosts": {"gateway": "gateway-dev.example.com"}}))
    assert any("admin.example.com" in w and "admin-api.example.com" in w
               for w in cfg.warnings())


def test_mantle_regions_rejects_non_list():
    with pytest.raises(schema.SchemaError):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tag": "x"},
            infra={"mantle_regions": {"us-east-1": True}}))


def test_mantle_regions_rejects_bad_items():
    for bad in ([None], ["Not A Region"], ["us_east_1"]):
        with pytest.raises(schema.SchemaError):
            schema.from_dict(minimal(
                deploy={"target": "eks", "size_tier": "t3"},
                images={"tag": "x"},
                infra={"mantle_regions": bad}))


def test_bedrock_arns_rejects_non_arn_items():
    with pytest.raises(schema.SchemaError):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tag": "x"},
            infra={"bedrock_allowed_model_arns": ["claude-sonnet"]}))


def test_body_logging_omitted_is_none():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"}))
    assert cfg.features.body_logging is None


def test_body_logging_explicit_false_warns_manual_disable():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        features={"body_logging": False}))
    assert cfg.features.body_logging is False
    assert any("비활성화" in w for w in cfg.warnings())


def test_mantle_regions_warns_on_ecs():
    cfg = schema.from_dict(minimal(
        deploy={"target": "ecs", "size_tier": "t2"},
        images={"tag": "x"},
        infra={"mantle_regions": ["us-east-1"]}))
    assert any("eks 전용" in w for w in cfg.warnings())


def test_mantle_regions_partition_ids():
    # gov/iso 파티션 리전 ID 도 리전 형식으로 인정한다.
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        infra={"mantle_regions": ["us-gov-west-1", "us-iso-east-1",
                                  "us-isob-east-1"]}))
    assert len(cfg.infra.mantle_regions) == 3


def test_body_logging_false_warns_only_on_eks():
    # ecs 는 tfvars 에 false 가 실제 적용되므로 "수동 비활성화" 경고는 부정확 —
    # eks managed overlay 경로에서만 울린다.
    cfg = schema.from_dict(minimal(
        deploy={"target": "ecs", "size_tier": "t2"},
        images={"tag": "x"},
        features={"body_logging": False}))
    assert not any("비활성화" in w for w in cfg.warnings())


def test_domain_hosts_warns_on_non_eks():
    cfg = schema.from_dict(minimal(
        deploy={"target": "ecs", "size_tier": "t2"},
        images={"tag": "x"},
        domain={"name": "example.com",
                "hosts": {"gateway": "gw.example.com"}}))
    assert any("domain.hosts" in w and "eks" in w for w in cfg.warnings())


def test_domain_hosts_outside_domain_name_warns():
    # *.{name} 인증서가 못 덮는 호스트 — route53-acm 경로에서 사전 경고.
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "x"},
        domain={"mode": "route53-acm", "name": "example.com",
                "hosts": {"gateway": "gw.other.io",
                          "admin_ui": "a.other.io",
                          "admin_api": "api.other.io"}}))
    assert any("범위 밖" in w for w in cfg.warnings())
