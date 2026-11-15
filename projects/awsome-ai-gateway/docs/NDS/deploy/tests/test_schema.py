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


def test_eks_oidc_without_domain_warns_lockout():
    # oidc 선언 + domain 없음 — hosted UI 콜백은 https 만 허용해 redirect
    # 거절 + dev_login 강제 off → 관리자 로그인 경로가 전혀 없다.
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "abc123"},
        oidc={"issuer_url": "https://x", "client_id": "c",
              "authorize_url": "https://x/a", "token_url": "https://x/t"}))
    assert any("로그인" in w or "콜백" in w for w in cfg.warnings())


def test_eks_oidc_with_domain_no_lockout_warning():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "abc123"},
        domain={"mode": "route53-acm", "name": "example.com",
                "zone_id": "Z123456789012"},
        oidc={"issuer_url": "https://x", "client_id": "c",
              "authorize_url": "https://x/a", "token_url": "https://x/t"}))
    assert not any("콜백" in w for w in cfg.warnings())


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


def test_eks_tag_covers_migration_unless_pinned():
    """7차 — images.tag 는 migration 도 커버한다 (push 가 migration:X 를
    빌드하므로 render 도 같은 태그를 발산해야 ImagePullBackOff 가 안 난다).
    개별 핀은 images.tags.migration."""
    from deploy.render import eks as eks_render
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "1.0.87-us19"}))
    v = eks_render.desired_values(cfg)
    assert v["migration"]["image"]["tag"] == "1.0.87-us19"
    cfg2 = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3"},
        images={"tag": "1.0.87-us19", "tags": {"migration": "1.0.54-us19"}}))
    v2 = eks_render.desired_values(cfg2)
    assert v2["migration"]["image"]["tag"] == "1.0.54-us19"


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


def test_sizing_eks_rejects_ecs_style_keys():
    """eks 도 sizing 을 소비한다 — 다만 키 어휘가 다르다. ecs 키(cpu)를 쓰면
    eks 허용 키 목록을 보여주며 거부한다."""
    with pytest.raises(schema.SchemaError, match="bedrock_thread_pool_size"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3",
                    "sizing": {"gateway-proxy": {"cpu": 2048}}},
            images={"tag": "abc"}))


def test_sizing_valid_on_eks():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3",
                "sizing": {"gateway-proxy": {"min_replicas": 3, "max_replicas": 30},
                           "redis": {"pool_size": 150}}},
        images={"tag": "abc"}))
    assert cfg.deploy.sizing["redis"]["pool_size"] == 150


def test_sizing_eks_unknown_service_rejected():
    with pytest.raises(schema.SchemaError, match="알 수 없는 서비스"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3",
                    "sizing": {"admin-api": {"min_replicas": 2}}},
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


def test_capacity_tier_valid_on_eks():
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3", "capacity_tier": "6000"},
        images={"tag": "abc"}))
    assert cfg.deploy.capacity_tier == "6000"


def test_capacity_tier_unknown_rejected():
    with pytest.raises(schema.SchemaError, match="capacity_tier"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3", "capacity_tier": "777"},
            images={"tag": "abc"}))


def test_capacity_tier_valid_on_ecs():
    cfg = schema.from_dict(minimal(
        deploy={"target": "ecs", "size_tier": "t2", "capacity_tier": "3000"},
        images={"tag": "abc"}))
    assert cfg.deploy.capacity_tier == "3000"


def test_capacity_tier_on_compose_rejected():
    with pytest.raises(schema.SchemaError, match="compose"):
        schema.from_dict(minimal(
            deploy={"target": "compose", "size_tier": "t0",
                    "capacity_tier": "3000"},
            images={"tag": "abc"}))


def test_eks_sizing_min_gt_max_rejected():
    """병합 결과 min > max 는 HPA 가 API 서버에 거부된다 — 스키마에서 잡는다."""
    with pytest.raises(schema.SchemaError, match="min_replicas"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3",
                    "sizing": {"gateway-proxy": {"min_replicas": 4,
                                                 "max_replicas": 3}}},
            images={"tag": "abc"}))


def test_eks_capacity_tier_plus_sizing_min_overflow_rejected():
    """밴드(500 → max3) 위에 min 만 크게 올리면 병합 min > max."""
    with pytest.raises(schema.SchemaError, match="min_replicas"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3",
                    "capacity_tier": "500",
                    "sizing": {"gateway-proxy": {"min_replicas": 9}}},
            images={"tag": "abc"}))


def test_eks_sizing_min_only_passes_schema():
    """min 만 선언은 스키마 통과 — env overlay 와의 최종 합성은 render 가 검증."""
    cfg = schema.from_dict(minimal(
        deploy={"target": "eks", "size_tier": "t3",
                "sizing": {"gateway-proxy": {"min_replicas": 2}}},
        images={"tag": "abc"}))
    assert cfg.deploy.sizing["gateway-proxy"]["min_replicas"] == 2


def test_ecs_sizing_desired_gt_max_rejected():
    """밴드(3000 → desired3/max6) 위에 desired 20 — 상한 밖 desired 는
    스케일링이 되돌리므로 선언 자체가 모순."""
    with pytest.raises(schema.SchemaError, match="desired_count"):
        schema.from_dict(minimal(
            deploy={"target": "ecs", "size_tier": "t2",
                    "capacity_tier": "3000",
                    "sizing": {"gateway-proxy": {"desired_count": 20}}},
            images={"tag": "abc"}))


def _eks(**over):
    doc = {"version": 1, "env": "acme-test",
           "deploy": {"target": "eks", "size_tier": "t2"},
           "aws": {"region": "ap-northeast-2"},
           "images": {"tag": "x"}}
    doc.update(over)
    return doc


def test_alerting_emails_require_enabled():
    with pytest.raises(schema.SchemaError, match="emails"):
        schema.from_dict(_eks(features={"alerting": {"emails": ["ops@x.io"]}}))


def test_alerting_malformed_email():
    with pytest.raises(schema.SchemaError, match="이메일"):
        schema.from_dict(_eks(features={"alerting": {
            "enabled": True, "emails": "ops@x.io, nope"}}))


def test_alerting_compose_rejected():
    with pytest.raises(schema.SchemaError, match="compose"):
        schema.from_dict(minimal(features={"alerting": {"enabled": True}}))
    with pytest.raises(schema.SchemaError, match="compose"):
        schema.from_dict(minimal(features={"alerting": {"log_shipping": True}}))


def test_alerting_log_shipping_eks_only():
    with pytest.raises(schema.SchemaError, match="eks"):
        schema.from_dict(_eks(
            deploy={"target": "ecs", "size_tier": "t1"},
            features={"alerting": {"log_shipping": True}}))


def test_alerting_enabled_no_emails_warns():
    cfg = schema.from_dict(_eks(features={"alerting": {"enabled": True}}))
    assert any("emails" in w for w in cfg.warnings())


def test_alerting_unknown_key_rejected():
    with pytest.raises(schema.SchemaError, match="알 수 없는"):
        schema.from_dict(_eks(features={"alerting": {"enbaled": True}}))


def test_alerting_log_shipping_alone_ok_on_eks():
    cfg = schema.from_dict(_eks(features={"alerting": {"log_shipping": True}}))
    assert cfg.features.alerting.log_shipping is True
    assert cfg.features.alerting.enabled is False


def test_eks_empty_release_rejected():
    """빈 release 면 ALB 발견의 instance 라벨 필터가 외부 ingress 를 전부
    채택한다 — eks 경로에서는 스키마 에러."""
    with pytest.raises(schema.SchemaError, match="deploy.release"):
        schema.from_dict(_eks(deploy={"target": "eks", "size_tier": "t2",
                                      "release": "  "}))


def test_eks_empty_namespace_rejected():
    # 공백만 있는 값은 오타 가능성이라 에러 — 명시적 null/"" 는 기본값으로 정규화
    with pytest.raises(schema.SchemaError, match="deploy.namespace"):
        schema.from_dict(_eks(deploy={"target": "eks", "size_tier": "t2",
                                      "namespace": "  "}))


def test_eks_null_release_defaults_not_literal_none():
    """release: (null) 은 "None" 리터럴이 아니라 기본값으로 정규화돼야 한다 —
    그대로면 helm release 이름이 None 이 되고 라벨 필터가 아무것도 못 잡는다."""
    cfg = schema.from_dict(_eks(deploy={"target": "eks", "size_tier": "t2",
                                        "release": None, "namespace": None}))
    assert cfg.deploy.release == "llm-gateway"
    assert cfg.deploy.namespace == "llm-gateway"


def test_alerting_emails_deduped_normalized():
    """case-variant/중복 이메일은 소문자 정규화 후 dedupe — SNS 구독 중복 방지."""
    cfg = schema.from_dict(_eks(features={"alerting": {
        "enabled": True,
        "emails": ["Ops@X.io", "ops@x.io", "team@x.io"]}}))
    assert cfg.features.alerting.emails == ["ops@x.io", "team@x.io"]


def test_admin_groups_scalar_wraps_not_splits():
    """12차 — 스칼라 문자열은 char 분해가 아니라 1-항목 리스트로."""
    from deploy import schema
    cfg = schema.from_dict({
        "env": "dev", "aws": {"region": "ap-northeast-2"},
        "deploy": {"target": "eks", "size_tier": "t3",
                   "tfstate_bucket": "state-bucket", "tfstate_table": "lock-table"},
        "images": {"tag": "v1"},
        "oidc": {"admin_groups": "Claude_DevOps"}})
    assert cfg.oidc.admin_groups == ["Claude_DevOps"]


def test_reject_unmatched_groups_string_rejected():
    """12차 — 인용된 'false' 가 bool()으로 True 가 되는 극성 반전 방지."""
    from deploy import schema
    import pytest
    with pytest.raises(schema.SchemaError):
        schema.from_dict({
            "env": "dev", "aws": {"region": "ap-northeast-2"},
            "deploy": {"target": "eks", "size_tier": "t3",
                       "tfstate_bucket": "state-bucket", "tfstate_table": "lock-table"},
            "images": {"tag": "v1"},
            "oidc": {"reject_unmatched_groups": "false"}})


def test_subnet_cidrs_outside_vpc_rejected():
    """12차 — vpc_cidr /18 에서 파생 /24 가 밖으로 나가면 SchemaError."""
    from deploy import schema
    import pytest
    with pytest.raises(schema.SchemaError):
        schema.subnet_cidrs("10.30.128.0/18", 2)
    # /16 은 통과
    out = schema.subnet_cidrs("10.30.0.0/16", 2)
    assert out["private_subnet_cidrs"] == ["10.30.1.0/24", "10.30.2.0/24"]


# ==============================================================================
# R18 회귀 — validate()/from_dict() 크래시 경로가 SchemaError 로 수렴
# ==============================================================================

def test_bad_size_tier_raises_schemaerror_not_keyerror():
    """잘못된 size_tier 로 preset() 을 부르면 KeyError 가 validate 를 빠져나가
    오류 목록이 유실된다 — 반드시 SchemaError 로 모아야 한다."""
    with pytest.raises(schema.SchemaError, match="size_tier"):
        schema.from_dict(minimal(deploy={"target": "eks", "size_tier": "t9"},
                                 images={"tag": "1.0"}))


def test_bad_sizing_value_raises_schemaerror_not_typeerror():
    """sizing 값이 비-int 면 오류는 잡히지만 dict 에 남아 병합/예산 계산이
    TypeError/ValueError 로 크래시했다 — 계산형 검사를 게이트한다."""
    doc = minimal(deploy={"target": "eks", "size_tier": "t3",
                          "sizing": {"gateway-proxy": {"min_replicas": "abc",
                                                     "max_replicas": 3}}},
                  images={"tag": "1.0"})
    with pytest.raises(schema.SchemaError, match="정수"):
        schema.from_dict(doc)


def test_sizing_scalar_raises_schemaerror():
    """deploy.sizing: "x" — dict("x") 가 ValueError 로 날아들던 경로."""
    with pytest.raises(schema.SchemaError, match="sizing"):
        schema.from_dict(minimal(deploy={"target": "eks", "size_tier": "t3",
                                         "sizing": "x"},
                                 images={"tag": "1.0"}))


def test_nonlist_fields_raise_schemaerror_not_typeerror():
    """int/dict 형태의 list 필드는 TypeError/침묵키순회 대신 명시 오류."""
    for field_path in (
            {"aws": {"allowed_roles": 5}},
            {"infra": {"azs": 5}},
            {"infra": {"cognito_groups": {"a": 1}}},
            {"network": {"allowed_cidrs": 5}},
    ):
        doc = minimal()
        for k, v in field_path.items():
            doc.setdefault(k, {}).update(v)
        with pytest.raises(schema.SchemaError):
            schema.from_dict(doc)


def test_version_float_rejected():
    """version: 1.0 — 1.0 == 1 이 True 라 형태 검증 없으면 통과된다."""
    with pytest.raises(schema.SchemaError, match="version"):
        schema.from_dict(minimal(version=1.0))


def test_bool_numeric_fields_rejected():
    """min_acu: true → 1.0, smtp_port: true → 1 같은 bool-숫자 혼용 차단."""
    doc = minimal(features={"notifications": {"provider": "smtp",
                                              "smtp_host": "h",
                                              "smtp_port": True}})
    with pytest.raises(schema.SchemaError):
        schema.from_dict(doc)
    doc2 = minimal(deploy={"target": "ecs", "size_tier": "t1"},
                   images={"tag": "1.0"},
                   infra={"db": {"min_acu": True}})
    with pytest.raises(schema.SchemaError):
        schema.from_dict(doc2)


def test_domain_and_account_id_format():
    """캡처/수작업 쓰레기 값이 ingress/ACM 으로 번지는 걸 선언 단계에서 차단."""
    with pytest.raises(schema.SchemaError, match="domain.name"):
        schema.from_dict(minimal(
            domain={"mode": "route53-acm", "name": "bad,name,"}))
    with pytest.raises(schema.SchemaError, match="zone_id"):
        schema.from_dict(minimal(
            domain={"mode": "route53-acm", "name": "ok.io",
                    "zone_id": "not-a-zone"}))
    with pytest.raises(schema.SchemaError, match="account_id"):
        schema.from_dict(minimal(aws={"account_id": "1234"}))


# ==============================================================================
# R24 회귀 — fullmatch 후행개행 거절 / IPv6 vpc_cidr / azs 상한
# ==============================================================================

def test_trailing_newline_rejected_everywhere():
    """`match`+`$` 는 후행 개행을 허용한다 — tfvars/Caddyfile 주입 경로라
    fullmatch 로 거절해야 한다."""
    base = {"version": 1, "env": "acme-test",
            "deploy": {"target": "compose", "size_tier": "t0"}}
    for bad in ("acme-test\n", "prod\n"):
        doc = dict(base, env=bad)
        with pytest.raises(schema.SchemaError):
            schema.from_dict(doc)


def test_ipv6_vpc_cidr_rejected():
    """IPv6 vpc_cidr 는 subnet_cidrs 의 subnet_of 가 TypeError 로 죽는다 —
    스키마가 거절해야 한다."""
    doc = {"version": 1, "env": "acme-test",
           "deploy": {"target": "eks", "size_tier": "t0"},
           "aws": {"region": "ap-northeast-2", "account_id": "1" * 12},
           "images": {"tag": "x"},
           "infra": {"vpc_cidr": "fd00::/8"}}
    with pytest.raises(schema.SchemaError, match="IPv4"):
        schema.from_dict(doc)


def test_azs_over_54_rejected():
    """AZ 54개 초과는 파생 서브넷 세 번째 옥텟이 255 를 넘어 무효 CIDR."""
    doc = {"version": 1, "env": "acme-test",
           "deploy": {"target": "eks", "size_tier": "t0"},
           "aws": {"region": "ap-northeast-2", "account_id": "1" * 12},
           "images": {"tag": "x"},
           "infra": {"azs": [f"ap-northeast-2{chr(97 + i % 26)}{i}"
                             for i in range(60)]}}
    with pytest.raises(schema.SchemaError):
        schema.from_dict(doc)


def test_image_tags_reject_none_and_dict_coercion():
    """images.tags 의 YAML null/dict 가 str() 코어션으로 'None'/"{'x':1}"
    태그가 되면 ImagePullBackOff — 형식 검증으로 거부한다."""
    with pytest.raises(schema.SchemaError, match="images.tags"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tags": {"gatewayProxy": None}}))
    with pytest.raises(schema.SchemaError, match="images.tags"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tags": {"gatewayProxy": {"x": 1}}}))
    with pytest.raises(schema.SchemaError, match="images.tags"):
        schema.from_dict(minimal(
            deploy={"target": "eks", "size_tier": "t3"},
            images={"tags": {"gatewayProxy": "bad tag,with"}}))


def test_allowed_roles_rejects_comma():
    """28차 — ALLOWED_IAM_ROLES 가 ",".join() 이라 항목 안 콤마가 두 role 로
    갈라져 허가 목록이 조용히 넓어진다 — 콤마는 스키마 거부."""
    import pytest
    with pytest.raises(schema.SchemaError):
        schema.from_dict(minimal(aws={"allowed_roles": ["Evil,AdminRole"]}))
    with pytest.raises(schema.SchemaError):
        schema.from_dict(minimal(aws={"allowed_roles": "A,B"}))
    # 정상 케이스는 통과
    cfg = schema.from_dict(minimal(aws={"allowed_roles": ["TeamDevRole",
                                                        "arn:aws:iam::1:role/X"]}))
    assert len(cfg.aws.allowed_roles) == 2
