# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""모델 레벨 크로스계정 라우팅 해석 (migration 0049).

우선순위 계약을 고정한다: **모델(alias 행)의 account_role_arn 이 클라이언트
routing_profile 보다 우선**하고, 모델 ARN 이 없으면 프로필 라우팅이 그대로
적용된다(무회귀). ModelConfigSchema 는 Redis 에서 역직렬화되므로 0049 이전의
옛 캐시 엔트리(라우팅 필드 부재)도 깨지지 않아야 한다.
"""
from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.schemas.domain import (
    ApiFormat,
    ModelConfigSchema,
    ModelPricingSchema,
    ModelStatus,
    ProviderType,
)
from app.schemas.routing import RoutingProfileSchema
from app.services.router_service import (
    apply_model_account_route,
    candidate_route_region,
    model_account_route,
)

ARN = "arn:aws:iam::610156625835:role/gw-test-bedrock"


def _cfg(**kw) -> ModelConfigSchema:
    return ModelConfigSchema(
        alias="claude-sonnet-4-6",
        provider=ProviderType.BEDROCK,
        provider_model_id="us.anthropic.claude-sonnet-4-6",
        api_format=ApiFormat.BEDROCK_NATIVE,
        endpoint="us-east-1",
        pricing=ModelPricingSchema(
            input_per_1k=Decimal("0.003"), output_per_1k=Decimal("0.015")
        ),
        status=ModelStatus.ACTIVE,
        **kw,
    )


def _profile(**kw) -> RoutingProfileSchema:
    kw.setdefault("client", "claude-code")
    kw.setdefault("backend", "invoke")
    kw.setdefault("region", "ap-northeast-2")
    return RoutingProfileSchema(**kw)


# ── model_account_route ──


def test_route_none_when_no_arn():
    assert model_account_route(_cfg()) is None


def test_route_none_when_config_none():
    assert model_account_route(None) is None


def test_route_returns_triple():
    cfg = _cfg(
        account_role_arn=ARN, region="ap-south-1", external_id="ext-9"
    )
    assert model_account_route(cfg) == (ARN, "ap-south-1", "ext-9")


def test_route_allows_missing_external_id():
    cfg = _cfg(account_role_arn=ARN, region="ap-south-1")
    assert model_account_route(cfg) == (ARN, "ap-south-1", None)


def test_route_arn_without_region_falls_back_loudly(capsys):
    """직접 SQL 로 ARN 만 넣은 행 — 대상 리전을 모르니 라우팅 미지정으로 읽는다.

    admin API 검증으로는 만들 수 없는 상태라 loud 로그로 남겨야 한다 —
    조용히 무시하면 "라우팅한 줄 알았는데 안 된" 운영 미스터리가 된다.
    (structlog 은 stdlib logging 을 거치지 않아 caplog 가 아닌 capsys 로 본다.)
    """
    cfg = _cfg(account_role_arn=ARN, region=None)
    assert model_account_route(cfg) is None
    assert "model_routing_incomplete_region" in capsys.readouterr().out


def test_stale_cache_schema_defaults_route_fields_to_none():
    """0049 이전의 Redis 캐시 JSON(라우팅 필드 부재)도 역직렬화돼야 한다.

    기본값이 없으면 배포 직후 stale 엔트리가 ValidationError → 모든 모델
    해석 500.
    """
    raw = {
        "alias": "claude-sonnet-4-6",
        "provider": "BEDROCK",
        "provider_model_id": "us.anthropic.claude-sonnet-4-6",
        "api_format": "BEDROCK_NATIVE",
        "endpoint": "us-east-1",
        "pricing": {"input_per_1k": "0.003", "output_per_1k": "0.015"},
        "status": "ACTIVE",
    }
    cfg = ModelConfigSchema.model_validate(raw)
    assert cfg.account_role_arn is None
    assert cfg.region is None
    assert cfg.external_id is None
    assert model_account_route(cfg) is None


# ── apply_model_account_route ──


def test_apply_returns_profile_unchanged_when_no_model_route():
    profile = _profile(account_role_arn="arn:aws:iam::111:role/a")
    cfg = _cfg()  # 모델 ARN 없음
    assert apply_model_account_route(profile, cfg) is profile


def test_apply_overrides_profile_route_with_model_route():
    """모델 ARN 이 프로필의 계정을 덮는다 — 우선순위 계약의 핵심."""
    profile = _profile(
        account_role_arn="arn:aws:iam::111:role/profile",
        external_id="profile-ext",
        web_search_enabled=True,
    )
    cfg = _cfg(
        account_role_arn=ARN, region="ap-south-1", external_id="model-ext"
    )
    merged = apply_model_account_route(profile, cfg, client="claude-code")

    assert merged.account_role_arn == ARN
    assert merged.region == "ap-south-1"
    assert merged.external_id == "model-ext"
    # 라우팅과 무관한 필드는 보존 — 백엔드/웹서치 플래그가 모델 라우팅에 덮이면 안 된다.
    assert merged.backend == "invoke"
    assert merged.web_search_enabled is True
    # 원본 프로필은 클라이언트 캐시 공유물 — 변형하면 같은 클라이언트의
    # 다른 모델 요청까지 오염된다.
    assert profile.account_role_arn == "arn:aws:iam::111:role/profile"


def test_apply_clears_external_id_when_model_omits_it():
    """프로필의 ExternalId 가 모델 라우팅에 새면 안 된다 — 다른 trust 조건."""
    profile = _profile(
        account_role_arn="arn:aws:iam::111:role/profile",
        external_id="profile-ext",
    )
    cfg = _cfg(account_role_arn=ARN, region="ap-south-1", external_id=None)
    merged = apply_model_account_route(profile, cfg)
    assert merged.external_id is None


def test_apply_synthesizes_profile_when_none():
    """프로필 없는 클라이언트 + 모델 ARN — 모델 라우팅만으로 크로스계정 성립."""
    cfg = _cfg(account_role_arn=ARN, region="ap-south-1")
    merged = apply_model_account_route(None, cfg, client="claude-code")
    assert merged is not None
    assert merged.account_role_arn == ARN
    assert merged.region == "ap-south-1"
    # backend="invoke" 여야 bedrock_account_client 분기 조건을 만족한다.
    assert merged.backend == "invoke"


def test_apply_none_profile_and_no_route_returns_none():
    cfg = _cfg()
    assert apply_model_account_route(None, cfg) is None


def test_apply_arn_without_region_leaves_profile_alone():
    """직접 SQL 불량 상태(ARN 있고 region 없음)는 라우팅 미지정 = 프로필 유지."""
    profile = _profile(account_role_arn="arn:aws:iam::111:role/a")
    cfg = _cfg(account_role_arn=ARN, region=None)
    assert apply_model_account_route(profile, cfg) is profile


class TestCandidateRouteRegion:
    """후보별 리전 해석 — 모델 > invoke 프로필 > in-account(None).

    회귀 방지: 원본 모델의 리전(_region_for_rewrite)을 ARN 없는 폴백 후보에
    그대로 쓰면, in-account 로 나갈 후보가 원본의 크로스계정 리전 접두로
    재작성되는 스플릿이 생겼다.
    """

    def test_model_route_wins_over_profile_region(self):
        profile = _profile(
            account_role_arn="arn:aws:iam::111:role/a", region="us-east-1"
        )
        cfg = _cfg(account_role_arn=ARN, region="ap-northeast-1")
        assert candidate_route_region(cfg, profile) == "ap-northeast-1"

    def test_no_model_route_falls_back_to_invoke_profile_region(self):
        profile = _profile(
            backend="invoke",
            account_role_arn="arn:aws:iam::111:role/a",
            region="ap-northeast-2",
        )
        cfg = _cfg()
        assert candidate_route_region(cfg, profile) == "ap-northeast-2"

    def test_unrouted_candidate_is_none_not_originals_region(self):
        """ARN 없는 후보는 None — 원본 모델의 리전을 물려받지 않는다."""
        cfg = _cfg()
        assert candidate_route_region(cfg, None) is None
        # in-account 프로필(backend 만 invoke, ARN 없음)이어도 None.
        profile = _profile(backend="invoke")
        assert candidate_route_region(cfg, profile) is None

    def test_non_invoke_profile_does_not_route_region(self):
        """mantle 백엔드 프로필은 bedrock invoke 리전 해석에 영향 없음."""
        profile = _profile(
            backend="mantle",
            account_role_arn="arn:aws:iam::111:role/a",
            region="ap-northeast-1",
        )
        cfg = _cfg()
        assert candidate_route_region(cfg, profile) is None
