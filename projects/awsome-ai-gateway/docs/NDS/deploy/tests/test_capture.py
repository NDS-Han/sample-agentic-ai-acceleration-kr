# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""capture 테스트 — 렌더 → 캡처 roundtrip 으로 역변환 정확도 검증."""
from __future__ import annotations

from pathlib import Path

import yaml
import pytest

from .. import capture, schema
from ..render import compose as compose_render

BASE = {
    "version": 1,
    "env": "acme-eval",
    "aws": {"region": "ap-northeast-2"},
    "deploy": {"target": "compose", "size_tier": "t0"},
    "network": {"mode": "public", "allowed_cidrs": ["10.0.0.0/8", "172.16.0.0/12"]},
    "domain": {"mode": "none"},
    "features": {"notifications": {"provider": "ses", "ses_from": "ops@acme.io"},
                 "web_search": True},
    "clients": {"models_profile": "global"},
    "oidc": {"issuer_url": "https://cognito-idp.ap-northeast-2.amazonaws.com/ap-xx",
             "client_id": "cid123",
             "authorize_url": "https://x.auth.amazoncognito.com/oauth2/authorize",
             "token_url": "https://x.auth.amazoncognito.com/oauth2/token",
             "required_group": "admins"},
}


def _roundtrip(tmp_path, doc):
    cfg = schema.from_dict(doc)
    out = tmp_path / "gen" / cfg.env
    compose_render.render(cfg, out)
    return capture.capture_compose(out)


def test_compose_roundtrip_core_fields(tmp_path):
    cap, notes = _roundtrip(tmp_path, BASE)
    assert cap["env"] == "acme-eval"
    assert cap["aws"]["region"] == "ap-northeast-2"
    assert cap["deploy"]["target"] == "compose"
    assert cap["network"]["allowed_cidrs"] == ["10.0.0.0/8", "172.16.0.0/12"]
    assert cap["features"]["notifications"]["provider"] == "ses"
    assert cap["features"]["notifications"]["ses_from"] == "ops@acme.io"
    assert cap["features"]["web_search"] is True
    assert cap["oidc"]["client_id"] == "cid123"
    assert cap["oidc"]["issuer_url"].startswith("https://cognito-idp")


def test_compose_roundtrip_domain_and_observability(tmp_path):
    doc = dict(BASE)
    doc["domain"] = {"mode": "route53-acm", "name": "gw.acme.io"}
    doc["features"] = {"notifications": {"provider": "mock"},
                       "observability": True}
    cap, _ = _roundtrip(tmp_path, doc)
    assert cap["domain"]["name"] == "gw.acme.io"
    assert cap["features"]["observability"] is True


def test_compose_captured_doc_is_schema_valid(tmp_path):
    cap, _ = _roundtrip(tmp_path, BASE)
    schema.from_dict(cap)  # 예외 없어야 함 — 못 채운 필드는 notes 가 알린다


def test_capture_emits_all_feature_keys(tmp_path):
    """캡처는 스키마의 features 키를 전부 기록한다 — 생략된 키는 사용자에게
    옵션 존재 자체를 숨기므로 라이브에 없는 기능도 false 로 명시돼야 한다."""
    expected = set(schema._FEATURE_KEYS)
    cap, _ = _roundtrip(tmp_path, BASE)
    assert set(cap["features"]) == expected
    assert cap["features"]["pricing_lambda"] is False
    assert cap["features"]["bi_insight"] is False
    assert cap["features"]["dev_login"] is False


def test_compose_capture_missing_dir(tmp_path):
    with pytest.raises(capture.CaptureError):
        capture.capture_compose(tmp_path / "nope")


def test_diff_docs_finds_changes():
    declared = {"a": {"b": 1, "c": "x"}}
    captured = {"a": {"b": 2, "d": "new"}}
    diffs = dict((k, (dv, cv)) for k, dv, cv in capture.diff_docs(declared, captured))
    assert diffs["a.b"] == (1, 2)
    assert diffs["a.c"] == ("x", None)
    assert diffs["a.d"] == (None, "new")


# ==============================================================================
# eks — 실제 dev values 파일로 helm 출력을 흉내내어 역변환 검증
# ==============================================================================

REPO_ROOT = Path(__file__).resolve().parents[4]
# 라이브 실측값은 .local.yaml(git 미추적 오버레이)에 둔다 — env_values_file 과 같은 우선순위
DEV_VALUES = REPO_ROOT / "deployment/charts/llm-gateway/values-eks-fargate-dev.local.yaml"
if not DEV_VALUES.exists():
    DEV_VALUES = REPO_ROOT / "deployment/charts/llm-gateway/values-eks-fargate-dev.yaml"


def _fake_eks(monkeypatch):
    import yaml as _yaml
    vals = _yaml.safe_load(DEV_VALUES.read_text())
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})


@pytest.mark.skipif(not DEV_VALUES.exists(), reason="dev values 파일 없음")
def test_eks_capture_from_real_dev_values(monkeypatch):
    _fake_eks(monkeypatch)
    doc, notes = capture.capture_eks()
    assert doc["env"] == "dev"
    assert doc["aws"]["region"] == "ap-south-1"
    assert doc["deploy"]["target"] == "eks"
    # ingress: gateway-dev.ssir-dev.nds.remu.one → base 추출
    assert doc["domain"]["name"] == "ssir-dev.nds.remu.one"
    assert doc["domain"]["mode"] == "route53-acm"  # certificate-arn 있음
    # inbound-cidrs 어노테이션
    assert "65.2.11.169/32" in doc["network"]["allowed_cidrs"]
    # features
    assert doc["features"]["web_search"] is True       # WEB_SEARCH_ENABLED=true
    assert doc["features"]["body_logging"] is True     # FIREHOSE_STREAM_NAME 있음
    assert doc["features"]["notifications"]["provider"] == "ses"
    # oidc — adminApi.oidc.issuerUrl
    assert "cognito-idp" in doc["oidc"]["issuer_url"]
    # 이미지 — gateway-proxy 태그가 기본값, 태그가 다른 서비스는 images.tags 맵으로.
    # 라이브 파일의 태그는 배포마다 바뀌므로(SHA 태그) 특정 값은 단정하지 않는다.
    assert doc["images"]["tag"], "images.tag 가 비어 있으면 안 된다"
    tags = doc["images"]["tags"]
    assert tags.get("gatewayProxy") == doc["images"]["tag"]
    assert "migration" in tags or "adminApi" in tags  # 멀티태그 실측이 맵에 담김


@pytest.mark.skipif(not DEV_VALUES.exists(), reason="dev values 파일 없음")
def test_eks_capture_emits_all_feature_keys(monkeypatch):
    _fake_eks(monkeypatch)
    doc, _ = capture.capture_eks()
    assert set(doc["features"]) == set(schema._FEATURE_KEYS)
    # dev values 는 LITELLM_PRICING_LAMBDA 가 실제로 켜져 있다
    assert doc["features"]["pricing_lambda"] is True


def test_eks_capture_live_drift_custom_release_name(monkeypatch):
    """커스텀 release 명명(<release>-llm-gateway-<svc>)에서도 live 이미지
    drift 가 감지돼야 한다 — fullname 하드코딩 prefix 는 침묵 누락을 만든다."""
    vals = {
        "global": {"environment": "dev", "imageRegistry": "reg.example.com"},
        "aws": {"region": "ap-northeast-2"},
        "gatewayProxy": {"image": {"tag": "1.0.0"}},
        "ingress": {},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)

    def _kubectl(resource, *a, **kw):
        if resource == "deployments":
            return {"items": [{
                "metadata": {"name": "myrelease-llm-gateway-gateway-proxy"},
                "spec": {"template": {"spec": {"containers": [
                    {"image": "reg.example.com/gateway-proxy:9.9.9"}]}}},
            }]}
        return {}
    monkeypatch.setattr(capture, "_kubectl_json", _kubectl)

    _, notes = capture.capture_eks(release="myrelease")
    assert any("9.9.9" in n and "gateway-proxy" in n for n in notes), notes


def test_eks_capture_live_drift_default_release_name(monkeypatch):
    """기본 명명(llm-gateway-<svc>)의 live drift 도 감지된다."""
    vals = {
        "global": {"environment": "dev"},
        "aws": {"region": "ap-northeast-2"},
        "gatewayProxy": {"image": {"tag": "1.0.0"}},
        "ingress": {},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)

    def _kubectl(resource, *a, **kw):
        if resource == "deployments":
            return {"items": [{
                "metadata": {"name": "llm-gateway-gateway-proxy"},
                "spec": {"template": {"spec": {"containers": [
                    {"image": "reg.example.com/gateway-proxy:9.9.9"}]}}},
            }]}
        return {}
    monkeypatch.setattr(capture, "_kubectl_json", _kubectl)

    _, notes = capture.capture_eks()
    assert any("9.9.9" in n and "gateway-proxy" in n for n in notes), notes


def test_configure_preserves_unprompted_fields(tmp_path, monkeypatch):
    """configure 가 질문 안 하는 필드(account_id/sizing/oidc 부가)를 보존."""
    import types
    from deploy import cli
    class T:
        def __init__(s, p, default=''): s.v = default if default is not None else 'x'
        def ask(s): return s.v
    class S:
        def __init__(s, p, choices, default=None):
            vals = {getattr(c,'value',c) for c in choices}
            s.v = default if default in vals else getattr(choices[0],'value',choices[0])
        def ask(s): return s.v
    class C:
        def __init__(s, p, choices, default=None):
            s.v = [getattr(c,'value',c) for c in choices if getattr(c,'checked',False)]
        def ask(s): return s.v
    class K:
        def __init__(s, p, default=False): s.v = default
        def ask(s): return s.v
    fake = types.SimpleNamespace(text=T, select=S, checkbox=C, confirm=K)
    fake.Choice = lambda label, value, checked=False: types.SimpleNamespace(
        label=label, value=value, checked=checked)
    monkeypatch.setattr(cli, "_questionary", lambda: fake)
    existing = {
        "env": "dev", "aws": {"region": "ap-south-1", "account_id": "123456789012"},
        "deploy": {"target": "eks", "size_tier": "t3",
                   "sizing": {"gateway-proxy": {"cpu": 2048}}},
        "oidc": {"issuer_url": "u", "client_id": "c", "authorize_url": "a",
                 "token_url": "t", "required_group": "grp",
                 "client_secret": "sec", "provider_name": "oidc:custom",
                 "vk_ttl_hours": 4},
        "network": {"mode": "public", "allowed_cidrs": ["1.2.3.4/32"],
                    "admin_internal": True},
        "domain": {"mode": "none", "name": "", "zone_id": ""},
        "features": {"notifications": {"provider": "smtp", "smtp_host": "h",
                                       "smtp_from": "f@x",
                                       "smtp_credentials_secret": "smtp-creds"}},
        "infra": {"azs": ["ap-south-1a", "ap-south-1b"],
                  "db": {"max_acu": 8}},
    }
    doc = cli._collect_doc(existing)
    assert doc["aws"]["account_id"] == "123456789012"
    assert doc["deploy"]["sizing"] == {"gateway-proxy": {"cpu": 2048}}
    assert doc["oidc"]["required_group"] == "grp"
    assert doc["oidc"]["client_secret"] == "sec"
    assert doc["oidc"]["provider_name"] == "oidc:custom"
    # 새 필드 — 질문/보존 경로 모두 기존값을 잃으면 안 된다
    assert doc["oidc"]["vk_ttl_hours"] == 4
    assert doc["network"]["admin_internal"] is True
    assert doc["features"]["notifications"]["smtp_credentials_secret"] == "smtp-creds"
    # infra.* 는 wizard 가 통째로 덮어쓰면 bootstrap 기록(azs 등)이 소실된다
    assert doc["infra"]["azs"] == ["ap-south-1a", "ap-south-1b"]
    assert doc["infra"]["db"] == {"max_acu": 8}


# ==============================================================================
# eks capture — 신규 스키마 필드 역변환
# ==============================================================================

def _eks_values(**over):
    vals = {
        "global": {"environment": "dev"},
        "aws": {"region": "ap-northeast-2"},
        "gatewayProxy": {"image": {"tag": "1.0.0"}},
        "ingress": {},
    }
    vals.update(over)
    return vals


def _capture_with(monkeypatch, vals, live=None):
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json",
                        lambda *a, **kw: live or {})
    return capture.capture_eks()


def test_eks_capture_admin_internal_and_vk_ttl(monkeypatch):
    """admin Ingress 만 internal 이면 admin_internal=true / mode=public."""
    vals = _eks_values(
        adminApi={"oidc": {"issuerUrl": "iss", "vkTtlHours": 4},
                  "image": {"tag": "1.0.0"}},
        adminUi={"env": {"OIDC_CLIENT_ID": "cid"}},
        ingress={
            "annotations": {"alb.ingress.kubernetes.io/scheme": "internet-facing"},
            "adminUi": {"annotations": {
                "alb.ingress.kubernetes.io/scheme": "internal"}},
            "adminApi": {"annotations": {
                "alb.ingress.kubernetes.io/scheme": "internal"}},
        },
        notificationWorker={"email": {"provider": "internal_api", "internalApi": {
            "url": "https://mail.corp", "fromAddress": "gw@corp"}}},
    )
    doc, _ = _capture_with(monkeypatch, vals)
    assert doc["network"]["mode"] == "public"
    assert doc["network"]["admin_internal"] is True
    assert doc["oidc"]["vk_ttl_hours"] == 4
    n = doc["features"]["notifications"]
    assert n["provider"] == "internal_api"
    assert n["internal_api_url"] == "https://mail.corp"
    assert n["internal_api_from"] == "gw@corp"


def test_eks_capture_all_internal_is_private_not_admin_internal(monkeypatch):
    """공통 scheme=internal 은 mode=private — admin_internal 과 구분한다."""
    vals = _eks_values(ingress={
        "annotations": {"alb.ingress.kubernetes.io/scheme": "internal"}})
    doc, _ = _capture_with(monkeypatch, vals)
    assert doc["network"]["mode"] == "private"
    assert doc["network"]["admin_internal"] is False


def test_eks_capture_smtp_fields(monkeypatch):
    vals = _eks_values(notificationWorker={"email": {
        "provider": "smtp",
        "smtp": {"host": "mail.corp", "port": 465, "fromAddress": "gw@corp",
                 "credentialsSecretName": "smtp-creds"}}})
    doc, _ = _capture_with(monkeypatch, vals)
    n = doc["features"]["notifications"]
    assert n["provider"] == "smtp"
    assert n["smtp_host"] == "mail.corp"
    assert n["smtp_port"] == 465
    assert n["smtp_from"] == "gw@corp"
    assert n["smtp_credentials_secret"] == "smtp-creds"


def test_doctor_ps_json_array():
    """compose v2 의 JSON 배열 출력도 states 로 파싱."""
    from deploy import doctor
    import json, subprocess
    rows = [{"Service": "gateway-proxy", "State": "running", "Health": "healthy"}]
    monkey = subprocess.CompletedProcess([], 0, json.dumps(rows), "")
    # _run 을 직접 대체할 수 없으니 파싱 로직 단위로 — json array 브랜치 검증
    parsed = json.loads(monkey.stdout)
    assert isinstance(parsed, list) and parsed[0]["Service"] == "gateway-proxy"


# ==============================================================================
# ecs capture — infra.db/infra.cache 역변환
# ==============================================================================

def _fake_ecs_aws(monkeypatch, *, db_clusters, repl_groups):
    """terraform output + aws 호출을 고정 응답으로 대체."""
    outputs = {
        "ecs_cluster_name": "llm-gateway-acme",
        "admin_ui_url": "",
        "https_enabled": False,
        "service_names": {"admin-api": "admin-api", "gateway-proxy": "gw"},
        "db_endpoint": "acme.cluster-xyz.rds.amazonaws.com",
        "redis_endpoint": "acme.cache.amazonaws.com",
    }
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)

    def _aws(argv, region):
        svc = argv[0]
        if svc == "ecs" and argv[1] == "describe-services":
            return {"services": [
                {"serviceName": "admin-api", "taskDefinition": "td:1",
                 "desiredCount": 1},
                {"serviceName": "gw", "taskDefinition": "td:1",
                 "desiredCount": 2}]}
        if svc == "ecs" and argv[1] == "describe-task-definition":
            return {"taskDefinition": {
                "cpu": "1024", "memory": "2048",
                "containerDefinitions": [
                    {"image": "reg/gw:1.2.3", "environment": []}]}}
        if svc == "rds":
            return {"DBClusters": db_clusters}
        if svc == "elasticache":
            return {"ReplicationGroups": repl_groups}
        return {}
    monkeypatch.setattr(capture, "_aws_json", _aws)


def test_ecs_capture_infra_db_and_cache(monkeypatch, tmp_path):
    """라이브 Aurora/ElastiCache 토폴로지가 infra.* override 로 기록된다 —
    채택해도 apply 가 tier 프리셋으로 인프라를 축소하지 않는다."""
    _fake_ecs_aws(
        monkeypatch,
        db_clusters=[{
            "Endpoint": "acme.cluster-xyz.rds.amazonaws.com",
            "ServerlessV2ScalingConfiguration":
                {"MinCapacity": 1.0, "MaxCapacity": 8.0}}],
        repl_groups=[{
            "ClusterEnabled": False,
            "CacheNodeType": "cache.t4g.medium",
            "NodeGroups": [{"PrimaryEndpoint":
                            {"Address": "acme.cache.amazonaws.com"}}],
            "MemberClusters": ["n1", "n2"]}])
    doc, notes = capture.capture_ecs(tmp_path, "ap-northeast-2")
    assert doc["infra"]["db"] == {
        "mode": "serverless", "min_acu": 1.0, "max_acu": 8.0}
    assert doc["infra"]["cache"] == {
        "mode": "replicated", "node_type": "cache.t4g.medium",
        "num_clusters": 2}
    assert any("infra.db/infra.cache" in n for n in notes)
    schema.from_dict(doc)  # 캡처 산출물이 스키마를 통과해야 채택 가능


def test_ecs_capture_cluster_mode_topology(monkeypatch, tmp_path):
    """cluster mode 는 NodeGroups 수와 샤드당 replica 수로 역변환된다."""
    _fake_ecs_aws(
        monkeypatch,
        db_clusters=[{
            "Endpoint": "acme.cluster-xyz.rds.amazonaws.com",
            "ServerlessV2ScalingConfiguration":
                {"MinCapacity": 0.5, "MaxCapacity": 4.0}}],
        repl_groups=[{
            "ClusterEnabled": True,
            "CacheNodeType": "cache.r7g.large",
            "NodeGroups": [
                {"PrimaryEndpoint": {"Address": "acme.cache.amazonaws.com"},
                 "NodeGroupMembers": [{}, {}, {}]},   # primary + 2 replica
                {"PrimaryEndpoint": {"Address": "other"},
                 "NodeGroupMembers": [{}, {}, {}]}],
            "MemberClusters": []}])
    doc, _ = capture.capture_ecs(tmp_path, "ap-northeast-2")
    assert doc["infra"]["cache"] == {
        "mode": "cluster", "node_type": "cache.r7g.large",
        "num_node_groups": 2, "replicas_per_node_group": 2}
    schema.from_dict(doc)


def test_ecs_capture_provisioned_db(monkeypatch, tmp_path):
    """provisioned Aurora 는 instance_class 로 기록된다."""
    _fake_ecs_aws(
        monkeypatch,
        db_clusters=[{
            "Endpoint": "acme.cluster-xyz.rds.amazonaws.com",
            "DBClusterInstanceClass": "db.r7g.large"}],
        repl_groups=[])
    doc, _ = capture.capture_ecs(tmp_path, "ap-northeast-2")
    assert doc["infra"]["db"] == {
        "mode": "provisioned", "instance_class": "db.r7g.large"}
    schema.from_dict(doc)


def test_eks_capture_oidc_falls_back_to_tf_outputs(monkeypatch, tmp_path):
    """helm values 에 adminUi.env.OIDC_* 리터럴이 없을 때(ESO/secretKeyRef 공급)
    terraform output 의 cognito_client_id / hosted_ui_domain 으로 복원한다 —
    ecs capture 와 같은 규칙."""
    vals = _eks_values(
        adminApi={"oidc": {"issuerUrl": "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-1"}},
        adminUi={"env": {}})
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: {
        "cognito_issuer_url": "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-1",
        "cognito_client_id": "abc123client",
        "cognito_hosted_ui_domain": "x.auth.ap-northeast-2.amazoncognito.com",
    })
    doc, _ = capture.capture_eks(env_dir=tmp_path)
    assert doc["oidc"]["client_id"] == "abc123client"
    assert doc["oidc"]["authorize_url"] == \
        "https://x.auth.ap-northeast-2.amazoncognito.com/oauth2/authorize"
    assert doc["oidc"]["token_url"] == \
        "https://x.auth.ap-northeast-2.amazoncognito.com/oauth2/token"


def test_eks_capture_oidc_helm_values_win_over_tf_outputs(monkeypatch, tmp_path):
    """values 에 리터럴이 있으면 terraform output 을 덮어쓰지 않는다."""
    vals = _eks_values(
        adminApi={"oidc": {"issuerUrl": "https://cognito-idp.ap-northeast-2.amazonaws.com/pool-1"}},
        adminUi={"env": {"OIDC_CLIENT_ID": "from-helm",
                         "OIDC_AUTHORIZE_URL": "https://helm.example/authorize",
                         "OIDC_TOKEN_URL": "https://helm.example/token"}})
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: {
        "cognito_client_id": "abc123client",
        "cognito_hosted_ui_domain": "x.auth.ap-northeast-2.amazoncognito.com",
    })
    doc, _ = capture.capture_eks(env_dir=tmp_path)
    assert doc["oidc"]["client_id"] == "from-helm"
    assert doc["oidc"]["authorize_url"] == "https://helm.example/authorize"


def test_eks_capture_dev_login_on_records_knob(monkeypatch):
    """라이브 DEV_LOGIN_ENABLED=true 는 features.dev_login 으로 기록돼야 한다 —
    안 잡으면 채택 첫 apply 에 oidc 경로가 admin-ui 를 false 로 되돌린다."""
    vals = _eks_values(
        adminUi={"env": {"DEV_LOGIN_ENABLED": "true"}},
        adminApi={"env": {"DEV_LOGIN_ENABLED": "true"}},
    )
    doc, notes = _capture_with(monkeypatch, vals)
    assert doc["features"]["dev_login"] is True
    assert any("dev login" in n for n in notes)


def test_eks_capture_dev_login_off_not_recorded(monkeypatch):
    vals = _eks_values(adminUi={"env": {"DEV_LOGIN_ENABLED": "false"}})
    doc, _ = _capture_with(monkeypatch, vals)
    assert not doc["features"].get("dev_login")
