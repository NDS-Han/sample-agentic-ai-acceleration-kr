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


def test_dev_login_true_pins_both_services(tmp_path):
    """features.dev_login=true — oidc 와 공존하는 개발자 로그인. adminUi 뿐 아니라
    adminApi 에도 켜야 dev. 토큰이 검증을 통과한다(한쪽만 켜면 API 호출 401)."""
    doc = dict(BASE, features={**BASE["features"], "dev_login": True})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["adminUi"]["env"]["DEV_LOGIN_ENABLED"] == "true"
    assert v["adminApi"]["env"]["DEV_LOGIN_ENABLED"] == "true"


def test_dev_login_default_off_with_oidc(tmp_path):
    """기본값 — oidc 가 있고 dev_login 선언이 없으면 admin-ui 는 끄고 adminApi 는
    env overlay 소유로 둔다(관여하지 않음)."""
    _, out, _ = _render(tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["adminUi"]["env"]["DEV_LOGIN_ENABLED"] == "false"
    assert "DEV_LOGIN_ENABLED" not in v.get("adminApi", {}).get("env", {})


def test_domain_hosts_override_beats_derived(tmp_path):
    """domain.hosts 가 있으면 gateway.{name} 파생 대신 선언 호스트를 쓴다 —
    기존 배포(gateway-dev.* 규칙) 채택 시 URL 보존이 목적이다."""
    doc = dict(BASE, domain={
        "mode": "route53-acm", "name": "gw.example.com",
        "hosts": {"gateway": "gateway-dev.gw.example.com",
                  "admin_ui": "admin-dev.gw.example.com",
                  "admin_api": "admin-api-dev.gw.example.com"}})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["ingress"]["gateway"]["host"] == "gateway-dev.gw.example.com"
    assert v["ingress"]["adminUi"]["host"] == "admin-dev.gw.example.com"
    assert v["ingress"]["adminApi"]["host"] == "admin-api-dev.gw.example.com"
    # NEXTAUTH_URL 도 선언 호스트로 — admin.{name} 파생이면 Cognito 콜백이 깨진다
    assert v["adminUi"]["nextauthUrl"] == "https://admin-dev.gw.example.com"


def test_managed_tfvars_carries_model_and_mantle_keys(tmp_path):
    doc = dict(BASE, infra={
        "bedrock_allowed_model_arns": [
            "arn:aws:bedrock:*::foundation-model/openai.*"],
        "mantle_regions": ["ap-northeast-1", "us-east-1"]})
    _, out, _ = _render(tmp_path, doc)
    tfvars = (out / "terraform.tfvars").read_text()
    assert "foundation-model/openai.*" in tfvars
    assert 'mantle_regions = ["ap-northeast-1", "us-east-1"]' in tfvars


def test_managed_tfvars_only_emits_enable_keys_when_on(tmp_path):
    """기능 토글은 "켜기"만 발산한다 — yaml 미선언/명시 false 이면 키 자체를
    쓰지 않아 auto-load 되는 env tfvars(사람 소유)의 true 가 유지된다.
    false 를 쓰면 기존 인프라 destroy 계획이 된다 (kiro R1)."""
    # BASE: provider=ses → ses 키만, body_logging 미선언 → 키 없음
    _, out, _ = _render(tmp_path)
    tfvars = (out / "terraform.tfvars").read_text()
    assert "enable_notification_ses = true" in tfvars
    assert "enable_body_logging" not in tfvars
    # 명시 false 도 쓰지 않는다
    doc = dict(BASE, features={**BASE["features"], "body_logging": False})
    _, out, _ = _render(tmp_path, doc)
    assert "enable_body_logging" not in (out / "terraform.tfvars").read_text()
    # 켜면 발산
    doc = dict(BASE, features={**BASE["features"], "body_logging": True})
    _, out, _ = _render(tmp_path, doc)
    assert "enable_body_logging = true" in (out / "terraform.tfvars").read_text()
    # provider != ses → ses 키 없음 (mock 은 명시/기본 구분 불가 — 켜기만 관리)
    doc = dict(BASE, features={**BASE["features"],
                               "notifications": {"provider": "mock"}})
    _, out, _ = _render(tmp_path, doc)
    assert "enable_notification_ses" not in (out / "terraform.tfvars").read_text()


def test_values_yaml_quotes_numeric_like_tags(tmp_path):
    """전부 숫자인 sha 태그는 go-yaml 이 수로 파싱한다 — 반드시 인용돼야 한다."""
    doc = dict(BASE, images={"tag": "00991142"})
    _, out, _ = _render(tmp_path, doc)
    text = (out / "values.yaml").read_text()
    assert "'00991142'" in text or '"00991142"' in text
    # 문자 포함 sha 는 평문 유지
    doc = dict(BASE, images={"tag": "761a3e4b"})
    _, out, _ = _render(tmp_path, doc)
    assert "tag: 761a3e4b" in (out / "values.yaml").read_text()

def test_oidc_admin_bootstrap_overrides_only_declared_keys(tmp_path):
    """회귀: admin_emails 만 선언해도 adminBootstrap 가 통째 교체돼 env 의
    groups(ClaudeAdmin) 가 [] 로 지워졌다 — 관리자 잠김 경로. 선언 키만 쓴다."""
    doc = dict(BASE, oidc={**BASE["oidc"], "admin_emails": ["boss@x.io"]})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    ab = v["adminApi"]["adminBootstrap"]
    assert ab["emails"] == ["boss@x.io"]
    assert "groups" not in ab          # env overlay 의 groups 보존

    doc2 = dict(BASE, oidc={**BASE["oidc"], "admin_groups": ["SSIR_Admin"]})
    _, out2, _ = _render(tmp_path / "b", doc2)
    v2 = yaml.safe_load((out2 / "values.yaml").read_text())
    ab2 = v2["adminApi"]["adminBootstrap"]
    assert ab2["groups"] == ["SSIR_Admin"]
    assert "emails" not in ab2         # env overlay 의 emails 보존

    # 둘 다 선언하면 둘 다 오버라이드
    doc3 = dict(BASE, oidc={**BASE["oidc"],
                            "admin_groups": ["SSIR_Admin"],
                            "admin_emails": ["a@x.io"]})
    _, out3, _ = _render(tmp_path / "c", doc3)
    ab3 = yaml.safe_load((out3 / "values.yaml").read_text())["adminApi"]["adminBootstrap"]
    assert ab3 == {"groups": ["SSIR_Admin"], "emails": ["a@x.io"]}


def test_oidc_group_keys_render_and_default_claim_inference(tmp_path):
    """groups_claim/group_prefix/reject_unmatched_groups → adminApi.oidc.* —
    선언 없으면 provider_name 에서 claim 유추(기존 동작)."""
    doc = dict(BASE, oidc={**BASE["oidc"],
                           "groups_claim": "custom:groups",
                           "group_prefix": "SSIR_",
                           "reject_unmatched_groups": False})
    _, out, _ = _render(tmp_path, doc)
    o = yaml.safe_load((out / "values.yaml").read_text())["adminApi"]["oidc"]
    assert o["groupsClaim"] == "custom:groups"
    assert o["groupPrefix"] == "SSIR_"
    assert o["rejectUnmatchedGroups"] is False

    # 미선언 — provider_name(oidc:cognito)에서 cognito:groups 유추, 나머지 키 부재
    _, out2, _ = _render(tmp_path / "d", BASE)
    o2 = yaml.safe_load((out2 / "values.yaml").read_text())["adminApi"]["oidc"]
    assert o2["groupsClaim"] == "cognito:groups"
    assert "groupPrefix" not in o2 and "rejectUnmatchedGroups" not in o2


def test_service_keys_disabled_by_default(tmp_path):
    """features.service_keys 기본 꺼짐 — 미선언이면 admin-api env 에 false 를
    명시해 앱 기본(true)을 덮는다. UI 버튼은 목록 응답 필드로 숨긴다."""
    _, out, _ = _render(tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["adminApi"]["env"]["SERVICE_KEYS_ENABLED"] == "false"


def test_service_keys_enabled_emits_true(tmp_path):
    """opt-in — true 선언이 env overlay 의 임의 값도 이긴다(yaml 이 진실 소스)."""
    doc = dict(BASE, features={**BASE["features"], "service_keys": True})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["adminApi"]["env"]["SERVICE_KEYS_ENABLED"] == "true"


def test_eks_sizing_renders_hpa_executor_and_redis_pool(tmp_path):
    """deploy.sizing 의 eks 소비 — HPA·executor env·redis.poolSize 를
    gateway.yaml 이 소유해 env overlay 의 stale 값(구 poolSize=40 사고)을
    침묵 되돌리지 않게 한다."""
    doc = dict(BASE, deploy={**BASE["deploy"], "sizing": {
        "gateway-proxy": {"min_replicas": 3, "max_replicas": 30,
                          "bedrock_thread_pool_size": 128},
        "redis": {"pool_size": 150},
    }})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    asg = v["gatewayProxy"]["autoscaling"]
    assert asg["enabled"] is True
    assert asg["minReplicas"] == 3 and asg["maxReplicas"] == 30
    assert v["gatewayProxy"]["env"]["BEDROCK_THREAD_POOL_SIZE"] == "128"
    assert v["redis"]["poolSize"] == 150


def test_eks_sizing_absent_emits_nothing(tmp_path):
    """미선언이면 sizing 관련 키를 만들지 않는다 — env overlay 값이 그대로 승계."""
    _, out, _ = _render(tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert "autoscaling" not in v.get("gatewayProxy", {})
    assert "redis" not in v
    assert "BEDROCK_THREAD_POOL_SIZE" not in v["gatewayProxy"]["env"]


def test_eks_capacity_tier_expands_preset(tmp_path):
    """capacity_tier 한 줄이 프리셋을 전개한다 — sizing 명시 없이도 HPA+pool."""
    doc = dict(BASE, deploy={**BASE["deploy"], "capacity_tier": "6000"})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    asg = v["gatewayProxy"]["autoscaling"]
    assert asg["minReplicas"] == 4 and asg["maxReplicas"] == 8
    assert v["redis"]["poolSize"] == 150


def test_eks_capacity_tier_sizing_overrides_single_key(tmp_path):
    """프리셋이 바닥이고 sizing 의 명시 키만 오버라이드한다."""
    doc = dict(BASE, deploy={**BASE["deploy"],
                             "capacity_tier": "3000",
                             "sizing": {"redis": {"pool_size": 200}}})
    _, out, _ = _render(tmp_path, doc)
    v = yaml.safe_load((out / "values.yaml").read_text())
    asg = v["gatewayProxy"]["autoscaling"]
    assert asg["minReplicas"] == 3 and asg["maxReplicas"] == 6
    assert v["redis"]["poolSize"] == 200


def _fake_chart_env(tmp_path, env_yaml: str):
    """env_values_file 은 repo_root 기준으로 찾는다 — 가짜 루트에 env 파일."""
    chart = tmp_path / "deployment" / "charts" / "llm-gateway"
    chart.mkdir(parents=True)
    (chart / "values-eks-fargate-dev.yaml").write_text(env_yaml)


def test_executor_inflight_target_derived_from_sizing_pool(tmp_path):
    """sizing 이 bedrock_thread_pool_size 를 바꾸면 HPA executor 타깃도
    풀의 ~70% 로 파생해 gen 오버레이에 실린다 — env 에 선언된 낡은 타깃
    (128 풀용 90)이 pool 256 에 남으면 스케일 기준점이 어긋난다.
    파생은 '선언된 타깃을 정정'하는 역할이라 env 선언이 전제다."""
    _fake_chart_env(tmp_path, (
        "gatewayProxy:\n  autoscaling:\n    enabled: true\n"
        "    minReplicas: 1\n    maxReplicas: 3\n"
        "    executorInflightTarget: 90\n"))
    doc = dict(BASE, deploy={**BASE["deploy"], "sizing": {
        "gateway-proxy": {"bedrock_thread_pool_size": 200}}})
    cfg = schema.from_dict(doc)
    out = tmp_path / "gen" / cfg.env / "eks"
    eks_render.render(cfg, out, repo_root=tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["gatewayProxy"]["autoscaling"]["executorInflightTarget"] == 140


def test_executor_inflight_target_env_mismatch_overrides_and_warns(tmp_path, capsys):
    """sizing 으로 풀을 바꿨는데 env 타깃이 파생값과 다르면 gen(마지막
    레이어)이 파생값을 방출해 이기고 경고를 낸다 — "env 명시가 있으면
    파생 불발"이었던 구 의미론은 env 파일 전부에 키가 박혀 파생이 dead
    code 였다."""
    _fake_chart_env(tmp_path, (
        "gatewayProxy:\n  autoscaling:\n    enabled: true\n"
        "    minReplicas: 1\n    maxReplicas: 3\n"
        "    executorInflightTarget: 90\n"))
    doc = dict(BASE, deploy={**BASE["deploy"], "sizing": {
        "gateway-proxy": {"bedrock_thread_pool_size": 200}}})
    cfg = schema.from_dict(doc)
    out = tmp_path / "gen" / cfg.env / "eks"
    eks_render.render(cfg, out, repo_root=tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["gatewayProxy"]["autoscaling"]["executorInflightTarget"] == 140
    assert "executorInflightTarget" in capsys.readouterr().err


def test_executor_inflight_target_env_matching_no_warning(tmp_path, capsys):
    """env 타깃이 파생값과 같으면 경고 없이 방출한다."""
    _fake_chart_env(tmp_path, (
        "gatewayProxy:\n  autoscaling:\n    enabled: true\n"
        "    minReplicas: 1\n    maxReplicas: 3\n"
        "    executorInflightTarget: 140\n"))
    doc = dict(BASE, deploy={**BASE["deploy"], "sizing": {
        "gateway-proxy": {"bedrock_thread_pool_size": 200}}})
    cfg = schema.from_dict(doc)
    out = tmp_path / "gen" / cfg.env / "eks"
    eks_render.render(cfg, out, repo_root=tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert v["gatewayProxy"]["autoscaling"]["executorInflightTarget"] == 140
    assert capsys.readouterr().err == ""


def test_executor_inflight_target_not_derived_without_declaration(tmp_path, capsys):
    """executor-inflight HPA 는 옵트인 — 아무 데도 타깃 선언이 없으면
    sizing 풀만으로 파생값을 방출하지 않는다(5차 리뷰 m3). 차트 기본은
    키가 주석 처리된 리소스 HPA 라, 방출이 곧 기능 활성화가 돼버린다.
    파생은 '선언된 값을 고치는' 역할에 한정 — overlay 부재 케이스."""
    # _fake_chart_env 를 부르지 않는다 — overlay 부재 + 미선언.
    doc = dict(BASE, deploy={**BASE["deploy"], "sizing": {
        "gateway-proxy": {"bedrock_thread_pool_size": 200}}})
    cfg = schema.from_dict(doc)
    out = tmp_path / "gen" / cfg.env / "eks"
    eks_render.render(cfg, out, repo_root=tmp_path)
    v = yaml.safe_load((out / "values.yaml").read_text())
    assert "executorInflightTarget" not in (
        v["gatewayProxy"].get("autoscaling") or {})
    assert "executorInflightTarget" in capsys.readouterr().err
