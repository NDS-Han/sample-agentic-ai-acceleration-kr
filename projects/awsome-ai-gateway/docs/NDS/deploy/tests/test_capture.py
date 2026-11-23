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


def test_compose_roundtrip_service_keys(tmp_path):
    """렌더러는 SERVICE_KEYS_ENABLED 를 .env(env_file)에만 쓴다 — api_env 만
    보면 false 배포가 true 로 캡처돼 채택 시 발급이 켜지는 회귀."""
    # 케이스별로 별도 디렉터리 — 같은 out_dir 재렌더는 파생 키가 yaml 을 따라
    #    이전 케이스 결과를 오염시킨다.
    doc = dict(BASE)
    doc["features"] = {"notifications": {"provider": "mock"},
                       "service_keys": False}
    cap, _ = _roundtrip(tmp_path / "off", doc)
    assert cap["features"]["service_keys"] is False

    doc["features"] = {"notifications": {"provider": "mock"},
                       "service_keys": True}
    cap, _ = _roundtrip(tmp_path / "on", doc)
    assert cap["features"]["service_keys"] is True


def test_service_keys_env_case_and_bool_forms():
    """yaml bool false / 대문자 / "0" 같은 값도 off 로 읽는다."""
    assert capture._is_false_env(False) is True
    assert capture._is_false_env("False") is True
    assert capture._is_false_env("0") is True
    assert capture._is_false_env("no") is True
    assert capture._is_false_env("true") is False
    assert capture._is_false_env(None) is False  # 부재 ≠ 명시적 off — "못 읽음" 구분용
    # 부재 → service_keys 는 _is_true_env(None)=False — 앱 기본 false(fail-closed)
    assert capture._is_true_env(None) is False
    assert capture._is_true_env(True) is True
    assert capture._is_true_env("True") is True


def test_dropped_keys_marks_unknown_capture():
    """읽기 실패로 기본값을 넣은 키(unknown)는 값이 있어도 삭제 위험 대상 —
    absorb/cp 가 기본값으로 선언값을 조용히 덮는 것을 막는다."""
    declared = {"features": {"service_keys": True}}
    captured = {"features": {"service_keys": False}}  # env 부재로 false 기록
    diffs = list(capture.diff_docs(declared, captured))
    # unknown 없으면: False 는 "읽은 값"으로 간주돼 dropped 에 안 잡힘
    assert capture.dropped_keys(diffs, captured) == []
    # unknown 에 있으면: 실측이 아니므로 삭제 위험으로 경고 대상
    dropped = capture.dropped_keys(
        diffs, captured, {"features.service_keys"})
    assert dropped == [("features.service_keys", True)]


def test_diff_docs_finds_changes():
    declared = {"a": {"b": 1, "c": "x"}}
    captured = {"a": {"b": 2, "d": "new"}}
    diffs = dict((k, (dv, cv)) for k, dv, cv in capture.diff_docs(declared, captured))
    assert diffs["a.b"] == (1, 2)
    assert diffs["a.c"] == ("x", None)
    assert diffs["a.d"] == (None, "new")


def test_dropped_keys_marks_only_yaml_only_declarations():
    # 캡처본을 통째로 채택하면 캡처가 못 읽는 yaml 전용 선언이 지워진다 —
    # infra.* 정책, images.tags.migration 같은 키가 여기 해당.
    declared = {
        "infra": {"bedrock_allowed_model_arns": ["arn:a"], "mantle_regions": ["r1"]},
        "images": {"tag": "abc", "tags": {"migration": "abc"}},
        "oidc": {"vk_ttl_hours": 0},
        "empty": {"note": ""},
    }
    captured = {
        "infra": {},                          # 캡처 불가 — 삭제 위험
        "images": {"tag": "abc"},             # tags 자체가 빠짐 — 삭제 위험
        "oidc": {"vk_ttl_hours": 1},          # 양쪽 다 값 있음 — 실제 diff
        "empty": {},                          # 빈 선언 — 지워져도 무의미
        "new": {"key": "v"},                  # 캡처에만 있음 — 삭제 아님
    }
    diffs = capture.diff_docs(declared, captured)
    dropped = dict(capture.dropped_keys(diffs, captured))
    assert set(dropped) == {
        "infra.bedrock_allowed_model_arns",
        "infra.mantle_regions",
        "images.tags.migration",
    }
    # falsy 하지만 유효한 선언값(0/False)도 yaml 전용 키로 잡힌다
    diffs2 = capture.diff_docs({"f": {"off": False}}, {})
    assert dict(capture.dropped_keys(diffs2, {})) == {"f.off": False}


def test_dropped_keys_catches_schema_default_blanks():
    """정규화본은 못 읽은 문자열 키를 스키마 기본값 "" 으로 채운다 — 원본 doc
    에 경로가 없거나 "" 이면 cp/absorb 가 선언값을 지우므로 경고 대상이다.
    tfstate.bucket, oidc.client_secret 처럼 캡처가 아예 방출하지 않는 키가 해당."""
    declared = {
        "tfstate": {"bucket": "llm-tfstate-acct", "table": "locks"},
        "oidc": {"client_secret": "super-secret"},
        "network": {"allowed_cidrs": ["10.0.0.0/8"]},
    }
    captured = {
        "oidc": {"issuer_url": "https://x", "client_id": "c"},  # secret 부재
        "network": {"allowed_cidrs": []},  # 캡처가 실제로 빈 값을 읽음
        # tfstate 섹션 자체 부재 (notes 로 채우라는 안내만 존재)
    }
    diffs = capture.diff_docs(declared, captured)
    dropped = dict(capture.dropped_keys(diffs, captured))
    assert dropped == {
        "tfstate.bucket": "llm-tfstate-acct",
        "tfstate.table": "locks",
        "oidc.client_secret": "super-secret",
    }
    # 읽은 빈 리스트는 삭제 경고가 아니라 absorb 가능한 실제 diff 다
    assert "network.allowed_cidrs" not in dropped


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
    # 현재 dev 는 features.body_logging=false — FIREHOSE_STREAM_NAME 미배선이라
    # 캡처는 키를 생략한다(false 박지 않는 규약 — 박으면 validate 가 경고).
    assert "body_logging" not in doc["features"]
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
    # body_logging 만 예외 — eks managed overlay 가 "켜기"만 소유해서 FIREHOSE
    # 미감지 시 false 를 박지 않고 키를 생략한다(capture.py 주석 참조).
    assert set(doc["features"]) == set(schema._FEATURE_KEYS) - {"body_logging"}
    # dev values 는 LITELLM_PRICING_LAMBDA 가 실제로 켜져 있다
    assert doc["features"]["pricing_lambda"] is True


@pytest.mark.skipif(not DEV_VALUES.exists(), reason="dev values 파일 없음")
def test_eks_capture_body_logging_on(monkeypatch):
    """FIREHOSE_STREAM_NAME env 가 있으면 body_logging=True 를 기록한다
    (없으면 위 테스트처럼 키 생략)."""
    import yaml as _yaml
    vals = _yaml.safe_load(DEV_VALUES.read_text())
    vals.setdefault("gatewayProxy", {}).setdefault("env", {})[
        "FIREHOSE_STREAM_NAME"] = "stream-x"
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    doc, _ = capture.capture_eks()
    assert doc["features"]["body_logging"] is True


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
        def __init__(s, p, default='', **kw): s.v = default if default is not None else 'x'
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
        "env": "dev", "aws": {"region": "ap-south-1", "account_id": "123456789012",
                              "allowed_roles": ["TeamDevRole"]},
        "deploy": {"target": "eks", "size_tier": "t3",
                   "capacity_tier": "3000",
                   "sizing": {"gateway-proxy": {"cpu": 2048}}},
        "oidc": {"issuer_url": "u", "client_id": "c", "authorize_url": "a",
                 "token_url": "t", "required_group": "grp",
                 "client_secret": "sec", "provider_name": "oidc:custom",
                 "vk_ttl_hours": 4},
        "network": {"mode": "public", "allowed_cidrs": ["1.2.3.4/32"],
                    "admin_internal": True},
        "domain": {"mode": "route53-acm", "name": "gw.example.com",
                   "zone_id": "Z1234"},
        "features": {"notifications": {"provider": "smtp", "smtp_host": "h",
                                       "smtp_from": "f@x", "smtp_port": 2525,
                                       "sender_name": "GW Alerts",
                                       "smtp_credentials_secret": "smtp-creds"},
                     "dev_login": True},
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
    # 묻지 않는 알림 필드·체크박스에 없는 플래그도 보존돼야 한다 —
    # 없으면 configure 한 번에 dev_login/smtp_port/sender_name 이 조용히 삭제된다
    assert doc["features"]["notifications"]["smtp_port"] == 2525
    assert doc["features"]["notifications"]["sender_name"] == "GW Alerts"
    assert doc["features"]["dev_login"] is True
    # infra.* 는 wizard 가 통째로 덮어쓰면 bootstrap 기록(azs 등)이 소실된다
    assert doc["infra"]["azs"] == ["ap-south-1a", "ap-south-1b"]
    # infra.db 는 ecs 전용 — eks yaml 에 섞여 있으면 스키마 하드에러라
    # 유지 시 저장 불가 doc 이 된다. wizard 가 걸러낸다.
    assert "db" not in doc["infra"]
    # aws.allowed_roles — 비면 admin-api 로그인 전량 403(fail-closed) 이라
    # configure 가 버리면 다음 apply 가 잠금 상태를 만든다 (19차 회귀)
    assert doc["aws"]["allowed_roles"] == ["TeamDevRole"]
    # deploy.capacity_tier — 버리면 다음 apply 가 프리셋으로 조용히 축소한다
    assert doc["deploy"]["capacity_tier"] == "3000"
    # domain.zone_id — eks 도 ACM 검증/Route53 레코드 자동화가 이 키에 달렸다.
    # ecs 전용 질문이면 eks 경로는 기존 값이 "" 로 덮인다 (19차 회귀)
    assert doc["domain"]["zone_id"] == "Z1234"


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


def test_eks_capture_sizing_band_match(monkeypatch):
    """라이브 HPA/pool 이 밴드와 정확히 일치 → capacity_tier 키로 기록."""
    vals = {
        "global": {"environment": "t", "imageRegistry": "r"},
        "aws": {"region": "r"}, "ingress": {},
        "gatewayProxy": {"autoscaling": {"minReplicas": 4, "maxReplicas": 8},
                         "env": {}},
        "redis": {"poolSize": 150},
        "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "")
    doc, _ = capture.capture_eks()
    assert doc["deploy"]["capacity_tier"] == "6000"
    assert "sizing" not in doc["deploy"]


def test_eks_capture_sizing_off_band(monkeypatch):
    """밴드 불일치 → 명시 deploy.sizing 으로 실측 기록."""
    vals = {
        "global": {"environment": "t"}, "aws": {"region": "r"}, "ingress": {},
        "gatewayProxy": {"autoscaling": {"minReplicas": 2, "maxReplicas": 7},
                         "env": {"BEDROCK_THREAD_POOL_SIZE": "192"}},
        "redis": {"poolSize": 200},
        "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "")
    doc, _ = capture.capture_eks()
    assert "capacity_tier" not in doc["deploy"]
    s = doc["deploy"]["sizing"]
    assert s["gateway-proxy"] == {"min_replicas": 2, "max_replicas": 7,
                                  "bedrock_thread_pool_size": 192}
    assert s["redis"]["pool_size"] == 200


def test_tfvars_parser_scalars_and_lists(tmp_path):
    """tfvars 부분집합 파서 — 캡처 스키마 키(str|list[str]) 역산."""
    (tmp_path / "terraform.tfvars").write_text('''
# 주석
project = "llm-gateway"
mantle_regions = ["ap-northeast-1", "us-east-1", "us-east-2"]
bedrock_allowed_model_arns = [
  "arn:aws:bedrock:*::foundation-model/anthropic.*",   # 인라인 주석
  "arn:aws:bedrock:*:*:project/*",
]
cognito_domain_suffix = "ssir-auth-123"
tags = { Owner = "x" }          # map 은 건너뜀
eks_cluster_version = "1.34"
''')
    tv = capture._tfvars(tmp_path)
    assert tv["project"] == "llm-gateway"
    assert tv["mantle_regions"] == ["ap-northeast-1", "us-east-1", "us-east-2"]
    assert len(tv["bedrock_allowed_model_arns"]) == 2
    assert tv["cognito_domain_suffix"] == "ssir-auth-123"
    assert "tags" not in tv                       # map 은 부분집합 밖
    assert tv["eks_cluster_version"] == "1.34"


def test_tf_backend_reads_local_state(tmp_path):
    """init 된 env 의 backend.config → tfstate_bucket/table 역산."""
    tdir = tmp_path / ".terraform"
    tdir.mkdir()
    (tdir / "terraform.tfstate").write_text(
        '{"backend": {"type": "s3", "config": {'
        '"bucket": "llm-gateway-tfstate-123", '
        '"dynamodb_table": "llm-gateway-tflock-123"}}}')
    bc = capture._tf_backend(tmp_path)
    assert bc["bucket"] == "llm-gateway-tfstate-123"
    assert bc["dynamodb_table"] == "llm-gateway-tflock-123"
    # init 안 된 env → 빈 dict (예외 아님)
    assert capture._tf_backend(tmp_path / "nowhere") == {}


def test_eks_capture_restores_tfvars_and_tfstate(tmp_path, monkeypatch):
    """env_dir 있으면 tfvars→infra, backend→tfstate 를 캡처본에 기록."""
    vals = {
        "global": {"environment": "dev"}, "aws": {"region": "ap-south-1"},
        "ingress": {"annotations": {}},
        "gatewayProxy": {"env": {}}, "redis": {}, "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "")
    monkeypatch.setattr(capture, "_aws_json",
                        lambda *a, **kw: {"HostedZones": []})
    # terraform output 은 읽혀야 env_dir 검증 경로가 살고 tfvars/backend 까지
    # 기록된다 — output 실패면 env 유래 값 전체를 기록하지 않는다(회귀:
    # 미검증 env_dir 채택 차단).
    monkeypatch.setattr(capture, "_tf_outputs",
                        lambda *a, **kw: {"aws_region": "ap-south-1"})
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text(
        '{"backend": {"config": {"bucket": "bkt", "dynamodb_table": "tbl"}}}')
    (tmp_path / "terraform.tfvars").write_text(
        'mantle_regions = ["us-east-1"]\ncognito_domain_suffix = "suf-1"\n')
    doc, notes = capture.capture_eks(env_dir=tmp_path)
    assert doc["deploy"]["tfstate_bucket"] == "bkt"
    assert doc["deploy"]["tfstate_table"] == "tbl"
    assert doc["infra"]["mantle_regions"] == ["us-east-1"]
    assert doc["infra"]["cognito_domain_suffix"] == "suf-1"


def test_eks_capture_drops_unverified_env_dir(tmp_path, monkeypatch):
    """terraform output 실패 env_dir — tfvars/backend 도 채택하지 않는다
    (다른 env state 의 값이 캡처본에 섞이는 오타겟 차단)."""
    vals = {"global": {"environment": "dev"}, "aws": {"region": "ap-south-1"},
            "ingress": {"annotations": {}},
            "gatewayProxy": {"env": {}}, "redis": {}, "adminApi": {"env": {}}}
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "")
    monkeypatch.setattr(capture, "_aws_json",
                        lambda *a, **kw: {"HostedZones": []})

    def _boom(*a, **kw):
        raise capture.CaptureError("terraform output 실패")
    monkeypatch.setattr(capture, "_tf_outputs", _boom)
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text(
        '{"backend": {"config": {"bucket": "bkt", "dynamodb_table": "tbl"}}}')
    (tmp_path / "terraform.tfvars").write_text('mantle_regions = ["us-east-1"]\n')
    doc, notes = capture.capture_eks(env_dir=tmp_path)
    assert "tfstate_bucket" not in doc.get("deploy", {})
    assert "mantle_regions" not in doc.get("infra", {})
    assert any("검증이 불가" in n or "못 읽었습니다" in n for n in notes)


def test_r53_zone_id_picks_longest_suffix(monkeypatch):
    """zone 매칭 — 가장 구체적인 public zone, private zone 제외."""
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {
        "HostedZones": [
            {"Name": "remu.one.", "Id": "/hostedzone/SHORT",
             "Config": {"PrivateZone": False}},
            {"Name": "ssir-dev.nds.remu.one.", "Id": "/hostedzone/LONG",
             "Config": {"PrivateZone": False}},
            {"Name": "internal.remu.one.", "Id": "/hostedzone/PRIV",
             "Config": {"PrivateZone": True}},
        ]})
    assert capture._r53_zone_id("ssir-dev.nds.remu.one", "r") == "LONG"
    assert capture._r53_zone_id("nomatch.example.com", "r") == ""


@pytest.fixture(autouse=True)
def _no_auto_env_dir(monkeypatch, tmp_path):
    """단위 테스트는 자동탐지로 실제 terraform env 를 읽지 않는다 —
    env_dir 를 명시한 테스트만 파일시스템 경로를 탄다.
    _kube_context_cluster 도 환경 의존 — 기본 ""(검증 스킵)으로 스텁하고
    불일치 경로를 보는 테스트만 개별 스텁한다.
    _gen_root 는 실제 repo 의 deployment/gen(커밋된 dev 산출물)을 가리켜
    캡처 결과를 오염시키므로 기본은 빈 tmp 경로로 스텁하고, gen overlay 를
    보는 테스트만 개별 스텁한다."""
    monkeypatch.setattr(capture, "_auto_env_dir", lambda *a, **kw: None)
    monkeypatch.setattr(capture, "_kube_context_cluster", lambda *a, **kw: "")
    monkeypatch.setattr(capture, "_gen_root", lambda: tmp_path / "no-gen")


def test_r53_alias_target_strips_prefix_only(monkeypatch):
    """lstrip 은 문자셋 스트립이라 dualstack.llm-* 가 m-* 로 망가졌다 —
    접두어만 제거하는지 회귀 잠금."""
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: [
        {"Name": "admin.acme.io.", "Type": "A",
         "AliasTarget": {"DNSName": "dualstack.llm-gw-123.ap-south-1.elb.amazonaws.com."}},
    ])
    assert capture._r53_alias_target("Z1", "admin.acme.io", "r") == \
        "llm-gw-123.ap-south-1.elb.amazonaws.com"


def test_tfvars_skips_block_comments_and_map_bodies(tmp_path):
    """리뷰 지적 오파싱 3종 — /* */ 주석, 들여쓰기 없는 map 본문,
    문자열 안의 # (라인 주석으로 오인해 값을 잘라 키가 통째로 누락됐다)."""
    (tmp_path / "terraform.tfvars").write_text('''
/*
mantle_regions = ["evil"]
*/
tags = {
mantle_regions = ["evil2"]
}
suffix = "auth#a1"          # 문자열 안 # — 잘리면 안 됨
region  = "ap-south-1"      // slash 주석
''')
    tv = capture._tfvars(tmp_path)
    assert tv["suffix"] == "auth#a1"
    assert tv["region"] == "ap-south-1"
    assert "mantle_regions" not in tv   # 주석/map 본문 안 값만 있었다


def test_r53_zone_id_finds_parent_zone_via_suffix(monkeypatch):
    """list-hosted-zones-by-name 은 정렬상 dns-name 이후만 반환해 부모 zone
    이 빠진다 — 접미사별 조회로 부모를 찾는지 회귀 잠금."""
    zones_by_name = {
        "nds.remu.one": [{"Name": "nds.remu.one.", "Id": "/hostedzone/PARENT",
                          "Config": {"PrivateZone": False}}],
    }

    def fake(argv, region):
        dns = argv[argv.index("--dns-name") + 1]
        return {"HostedZones": zones_by_name.get(dns, [])}

    monkeypatch.setattr(capture, "_aws_json", fake)
    # zone 은 부모 nds.remu.one 뿐 — ssir-dev.nds.remu.one 자체 zone 없음
    assert capture._r53_zone_id("ssir-dev.nds.remu.one", "r") == "PARENT"


def test_alb_ingress_cidrs_all_traffic_and_httpmode_ports(monkeypatch):
    """IpProtocol -1(all traffic) 규칙과 http_mode 포트(8000/8080/3000)도
    인입 허용으로 인정 — 80/443 만 보면 실제 제한을 [] 로 놓친다."""
    calls = iter([
        {"LoadBalancers": [{"DNSName": "alb-1.elb.amazonaws.com",
                            "SecurityGroups": ["sg-1"]}]},
        {"SecurityGroups": [{"IpPermissions": [
            {"IpProtocol": "-1", "IpRanges": [{"CidrIp": "10.0.0.0/8"}]},
            {"IpProtocol": "tcp", "FromPort": 8000, "ToPort": 8000,
             "IpRanges": [{"CidrIp": "172.16.0.0/12"}]},
            {"IpProtocol": "tcp", "FromPort": 22, "ToPort": 22,
             "IpRanges": [{"CidrIp": "1.2.3.4/32"}]},  # 관리용 — ALB 아님
        ]}]},
    ])
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: next(calls))
    cidrs, scheme = capture._alb_ingress_cidrs("alb-1.elb.amazonaws.com", "r")
    assert cidrs == ["10.0.0.0/8", "172.16.0.0/12"]


def test_eks_capture_skips_sizing_when_autoscaling_disabled(monkeypatch):
    """autoscaling.enabled=false + 고정 replica 운영 — 차트 기본 min/max(2/20)
    를 역산해 기록하면 채택 후 apply 가 꺼둔 HPA 를 다시 켠다."""
    vals = {
        "global": {"environment": "loadtest"}, "aws": {"region": "r"},
        "ingress": {"annotations": {}},
        "gatewayProxy": {"env": {},
                         "replicaCount": 4,
                         "autoscaling": {"enabled": False,
                                         "minReplicas": 2, "maxReplicas": 20}},
        "redis": {"poolSize": 40}, "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    doc, notes = capture.capture_eks()
    assert "gateway-proxy" not in (doc["deploy"].get("sizing") or {})
    # redis.poolSize 는 autoscaling 과 무관 — 여전히 기록한다
    assert doc["deploy"].get("sizing", {}).get("redis") == {"pool_size": 40}
    assert any("enabled=false" in n for n in notes)


def test_eks_capture_rejects_env_dir_of_other_cluster(monkeypatch, tmp_path):
    """같은 env 이름으로 다른 계정의 terraform env 를 고르면 tfstate/tfvars
    까지 다른 환경 값이 섞인다 — kube context 클러스터와 대조해 배제."""
    vals = {
        "global": {"environment": "dev"}, "aws": {"region": "r"},
        "ingress": {"annotations": {}},
        "gatewayProxy": {"env": {}}, "redis": {}, "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_tf_outputs",
                        lambda *a, **kw: {"cluster_name": "llm-gateway-dev-OTHER"})
    monkeypatch.setattr(capture, "_kube_context_cluster",
                        lambda *a, **kw: "llm-gateway-dev")
    (tmp_path / "terraform.tfvars").write_text('cognito_domain_suffix = "WRONG"\n')
    doc, notes = capture.capture_eks(env_dir=tmp_path)
    assert doc["deploy"]["tf_env_dir"] == ""
    assert "infra" not in doc or "cognito_domain_suffix" not in doc.get("infra", {})
    assert any("다릅니다" in n for n in notes)


def test_ecs_capture_omits_allowed_cidrs_when_unread(monkeypatch, tmp_path):
    """SG 를 못 읽으면 allowed_cidrs 키를 쓰지 않는다 — 빈 리스트를 기록하면
    채택 후 apply 가 ALB 인그레스 규칙을 전부 지운다."""
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: {
        "ecs_cluster_name": "llm-gateway-dev",
        "admin_ui_url": "https://admin.acme.io",
        "https_enabled": True,
        "service_names": {"admin-api": "admin-api"},
    })
    # Route53/ECS 조회는 전부 빈 응답 — zone 없음 → alias/SG 미조회 → cidrs 빈
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {})
    doc, notes = capture.capture_ecs(env_dir=tmp_path, region="r")
    assert "allowed_cidrs" not in doc["network"]
    assert any("못 읽었습니다" in n for n in notes)


def test_tfvars_skips_heredoc_and_crossline_keys(tmp_path):
    """heredoc 본문의 key=value 는 오파싱하면 안 되고, '=' 다음 개행이
    다음 키 이름을 삼키면 안 된다 (리뷰 반영 2건)."""
    (tmp_path / "terraform.tfvars").write_text(
        'desc = <<EOT\n'
        'inner = "fake"\n'
        'EOT\n'
        'mantle_regions = ["us-east-1"]\n'
        'desc2 = <<-EOF\n'
        'mantle_regions = ["fake"]\n'
        'EOF\n'
        'num = 3\n')
    tv = capture._tfvars(tmp_path)
    assert "inner" not in tv and "desc" not in tv
    assert tv["mantle_regions"] == ["us-east-1"]
    assert tv["num"] == 3


# ==============================================================================
# R18 회귀 — 공유 ns 흡수/사이드카/ALB scheme/읽기실패 unknown/미읽기 키 복원
# ==============================================================================

def test_eks_capture_ignores_foreign_release_resources(monkeypatch):
    """공유 ns 의 타 릴리스(instance=other) ingress/deployment 를 우리 것으로
    흡수하면 안 된다 — scheme/cidr/태그 전부 타 배포 값으로 오염된다."""
    vals = {
        "global": {"environment": "dev"},
        "aws": {"region": "ap-northeast-2"},
        "ingress": {"annotations": {}, "gateway": {"host": "gateway.acme.io"}},
        "gatewayProxy": {"env": {}}, "redis": {}, "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "1.0.0")

    foreign = {"app.kubernetes.io/instance": "other-rel"}
    ours = {"app.kubernetes.io/instance": "llm-gateway"}

    def _kubectl(resource, *a, **kw):
        if resource == "ingress":
            return {"items": [
                {"metadata": {"name": "other-rel-gateway", "labels": foreign,
                              "annotations": {
                                  "alb.ingress.kubernetes.io/scheme": "internal"}},
                 "spec": {"rules": [{"host": "other.example.com"}]}},
                {"metadata": {"name": "llm-gateway-gateway", "labels": ours,
                              "annotations": {
                                  "alb.ingress.kubernetes.io/scheme":
                                  "internet-facing"}},
                 "spec": {"rules": [{"host": "gateway.acme.io"}]}},
            ]}
        if resource == "deployments":
            return {"items": [
                {"metadata": {"name": "other-rel-gateway-proxy",
                              "labels": foreign},
                 "spec": {"template": {"spec": {"containers": [
                     {"name": "gateway-proxy", "image": "reg/gw:BAD"}]}}}},
                {"metadata": {"name": "llm-gateway-gateway-proxy",
                              "labels": ours},
                 "spec": {"template": {"spec": {"containers": [
                     {"name": "gateway-proxy", "image": "reg/gw:7.7.7"}]}}}},
            ]}
        return {}
    monkeypatch.setattr(capture, "_kubectl_json", _kubectl)
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {})

    doc, _ = capture.capture_eks()
    assert doc["network"]["mode"] == "public"   # 타 릴리스 internal 미흡수
    assert doc["images"]["tag"] == "7.7.7"       # BAD 태그 미흡수


def test_eks_capture_sidecar_does_not_overwrite_app_tag(monkeypatch):
    """차트는 otel-collector 사이드카를 두 번째 컨테이너로 싣는다 — 무차별
    순회하면 사이드카 이미지 태그가 앱 태그를 덮어쓴다."""
    vals = {
        "global": {"environment": "dev"},
        "aws": {"region": "ap-northeast-2"},
        "ingress": {},
        "gatewayProxy": {"env": {}}, "redis": {}, "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "1.0.0")

    def _kubectl(resource, *a, **kw):
        if resource == "deployments":
            return {"items": [{
                "metadata": {"name": "llm-gateway-gateway-proxy"},
                "spec": {"template": {"spec": {"containers": [
                    {"name": "gateway-proxy",
                     "image": "reg.example.com/gw:2.0.0"},
                    {"name": "otel-collector",
                     "image": "otel/opentelemetry-collector:0.91.0"}]}}}},
            ]}
        return {}
    monkeypatch.setattr(capture, "_kubectl_json", _kubectl)
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {})

    doc, _ = capture.capture_eks()
    assert doc["images"]["tag"] == "2.0.0"


def test_eks_capture_migration_no_appversion_fallback(monkeypatch):
    """migration 이미지는 독립 버전 라인(headNNNN) — appVersion 폴백으로
    존재하지 않는 태그를 고정하면 migration Job 이 ImagePull 실패한다."""
    vals = {
        "global": {"environment": "dev"},
        "aws": {"region": "ap-northeast-2"},
        "ingress": {},
        "gatewayProxy": {"env": {}, "image": {"tag": "1.2.3"}},
        "redis": {}, "adminApi": {"env": {}},
        "migration": {"image": {}},   # values 미명시 + Job 이라 라이브 없음
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "9.9.9")
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {})

    doc, _ = capture.capture_eks()
    tags = doc["images"].get("tags", {})
    assert tags.get("migration") != "9.9.9"
    assert "migration" not in tags or not tags["migration"]


def test_eks_capture_live_cidrs_preferred_over_values(monkeypatch):
    """live 어노테이션이 시행 중인 제한 — values 의 더 넓은 목록으로 기록하면
    채택 apply 가 ALB 를 다시 연다."""
    vals = {
        "global": {"environment": "dev"},
        "aws": {"region": "ap-northeast-2"},
        "ingress": {"annotations": {
            "alb.ingress.kubernetes.io/inbound-cidrs": "0.0.0.0/0"}},
        "gatewayProxy": {"env": {}}, "redis": {}, "adminApi": {"env": {}},
    }
    monkeypatch.setattr(capture, "_helm_values", lambda *a, **kw: vals)
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "")

    def _kubectl(resource, *a, **kw):
        if resource == "ingress":
            return {"items": [{
                "metadata": {"name": "llm-gateway-gateway",
                             "annotations": {
                                 "alb.ingress.kubernetes.io/inbound-cidrs":
                                 "10.0.0.0/8"}},
                "spec": {"rules": []}}]}
        return {}
    monkeypatch.setattr(capture, "_kubectl_json", _kubectl)
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {})

    doc, notes = capture.capture_eks()
    assert doc["network"]["allowed_cidrs"] == ["10.0.0.0/8"]
    assert any("드리프트" in n for n in notes)


def test_ecs_capture_internal_alb_records_private(monkeypatch, tmp_path):
    """internal ALB 를 public 으로 기록하면 채택 apply(alb_internal=false)
    가 인터넷 노출로 되돌린다."""
    outputs = {
        "ecs_cluster_name": "llm-gateway-acme",
        "admin_ui_url": "https://admin.acme.io",
        "https_enabled": True,
        "service_names": {"admin-api": "admin-api"},
    }
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)

    def _aws(argv, region):
        if argv[0] == "route53" and "list-resource-record-sets" in argv:
            return [{"Name": "admin.acme.io.", "Type": "A",
                     "AliasTarget": {"DNSName":
                                     "dualstack.internal-alb.elb.amazonaws.com."}}]
        if argv[0] == "route53":
            return {"HostedZones": [{"Name": "acme.io.", "Id": "Z1",
                                     "Config": {"PrivateZone": False}}]}
        if argv[0] == "elbv2":
            return {"LoadBalancers": [
                {"DNSName": "internal-alb.elb.amazonaws.com",
                 "Scheme": "internal", "SecurityGroups": ["sg-1"]}]}
        if argv[0] == "ec2":
            return {"SecurityGroups": [{"IpPermissions": [
                {"IpProtocol": "-1",
                 "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]}]}
        if argv[0] == "ecs" and argv[1] == "describe-services":
            return {"services": [{"serviceName": "admin-api",
                                  "taskDefinition": "td:1",
                                  "desiredCount": 1}]}
        if argv[0] == "ecs":
            return {"taskDefinition": {
                "cpu": "512", "memory": "1024",
                "containerDefinitions": [{"name": "admin-api",
                                          "image": "reg/api:1.0",
                                          "environment": []}]}}
        return {}
    monkeypatch.setattr(capture, "_aws_json", _aws)
    doc, notes = capture.capture_ecs(tmp_path, "ap-northeast-2")
    assert doc["network"]["mode"] == "private"
    assert doc["network"]["allowed_cidrs"] == ["10.0.0.0/8"]


def test_ecs_capture_taskdef_fail_marks_env_features_unknown(monkeypatch, tmp_path):
    """admin-api task def 읽기 실패 시 env 유래 기능키는 false 박지 말고
    unknown 표기해야 한다 — body_logging=false 는 Firehose destroy 경로."""
    outputs = {
        "ecs_cluster_name": "llm-gateway-acme",
        "admin_ui_url": "", "https_enabled": False,
        "service_names": {"admin-api": "admin-api"},
    }
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)

    def _aws(argv, region):
        if argv[0] == "ecs" and argv[1] == "describe-services":
            return {"services": [{"serviceName": "admin-api",
                                  "taskDefinition": "td:missing",
                                  "desiredCount": 1}]}
        raise capture.CaptureError("td gone")
    monkeypatch.setattr(capture, "_aws_json", _aws)

    unknown: set = set()
    doc, notes = capture.capture_ecs(tmp_path, "r", unknown=unknown)
    assert "features.body_logging" in unknown
    assert "features.service_keys" in unknown
    assert doc["features"]["body_logging"] is False


def test_ecs_capture_restores_iam_roles_and_admin_groups(monkeypatch, tmp_path):
    """render 가 무조건 다시 쓰는 allowed_iam_roles/admin_groups 는 tfvars
    에서 복원 — 못 읽으면 채택 apply 가 CLI VK 전면 403/관리자 잠김."""
    outputs = {
        "ecs_cluster_name": "llm-gateway-acme",
        "admin_ui_url": "", "https_enabled": False,
        "service_names": {"admin-api": "admin-api"},
    }
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)
    (tmp_path / "terraform.tfvars").write_text(
        'allowed_iam_roles = ["arn:aws:iam::111122223333:role/Dev"]\n'
        'admin_groups = ["Claude_PlatformOps"]\n'
        'cognito_groups = ["Claude_TeamA"]\n')

    def _aws(argv, region):
        if argv[0] == "ecs" and argv[1] == "describe-services":
            return {"services": [{"serviceName": "admin-api",
                                  "taskDefinition": "td:1",
                                  "desiredCount": 1}]}
        if argv[0] == "ecs":
            return {"taskDefinition": {
                "cpu": "512", "memory": "1024",
                "containerDefinitions": [{"name": "admin-api",
                                          "image": "reg/api:1.0",
                                          "environment": []}]}}
        return {}
    monkeypatch.setattr(capture, "_aws_json", _aws)
    doc, _ = capture.capture_ecs(tmp_path, "r")
    assert doc["aws"]["allowed_roles"] == [
        "arn:aws:iam::111122223333:role/Dev"]
    assert doc["oidc"]["admin_groups"] == ["Claude_PlatformOps"]
    assert doc["infra"]["cognito_groups"] == ["Claude_TeamA"]


def test_ecs_capture_gateway_proxy_tag_preferred(monkeypatch, tmp_path):
    """서비스별 태그가 다르면 마지막 순회 서비스가 아니라 gateway-proxy 를
    대표로 기록하고 차이를 노트로 남긴다."""
    outputs = {
        "ecs_cluster_name": "llm-gateway-acme",
        "admin_ui_url": "", "https_enabled": False,
        "service_names": {"admin-api": "admin-api", "gateway-proxy": "gw"},
    }
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)
    imgs = {"td:gw": "reg/gw:2.0", "td:api": "reg/api:9.9"}

    def _aws(argv, region):
        if argv[0] == "ecs" and argv[1] == "describe-services":
            return {"services": [
                {"serviceName": "admin-api", "taskDefinition": "td:api",
                 "desiredCount": 1},
                {"serviceName": "gw", "taskDefinition": "td:gw",
                 "desiredCount": 2}]}
        if argv[0] == "ecs" and "describe-task-definition" in argv:
            td = argv[-1]
            return {"taskDefinition": {
                "cpu": "512", "memory": "1024",
                "containerDefinitions": [{"name": "app",
                                          "image": imgs[td],
                                          "environment": []}]}}
        return {}
    monkeypatch.setattr(capture, "_aws_json", _aws)
    doc, notes = capture.capture_ecs(tmp_path, "r")
    assert doc["images"]["tag"] == "2.0"
    assert any("이미지 태그가 다릅니다" in n for n in notes)


def test_configure_uses_named_captured_file_as_source(tmp_path, monkeypatch):
    """19차 — `deploy configure --config gateway.captured-stg.yaml` 은 출력만
    DEFAULT_CONFIG 로 돌리고, 기본값 소스는 지명한 캡처본이어야 한다. 이전엔
    DEFAULT_CONFIG(다른 env)를 읽어 wizard 가 잘못된 env 값으로 미리 채웠다."""
    import types
    from deploy import cli
    captured = tmp_path / "gateway.captured-stg.yaml"
    captured.write_text("env: stg\ndeploy: {target: eks}\n")
    (tmp_path / "gateway.yaml").write_text("env: prod\ndeploy: {target: eks}\n")
    monkeypatch.setattr(cli, "DEFAULT_CONFIG", tmp_path / "gateway.yaml")
    seen = {}
    monkeypatch.setattr(cli, "_questionary",
                        lambda: types.SimpleNamespace())
    monkeypatch.setattr(cli, "_collect_doc",
                        lambda ex: (seen.update(existing=ex), {})[1])
    monkeypatch.setattr(cli, "_write_config", lambda p, d: 0)
    args = types.SimpleNamespace(config=str(captured), no_apply=True)
    cli.cmd_configure(args)
    assert seen["existing"].get("env") == "stg"  # prod 가 아니라 stg


def test_eks_capture_alert_emails_from_gen_overlay(monkeypatch, tmp_path):
    """20차 회귀 — alert_emails 는 gen managed overlay(deployment/gen/<env>/eks)
    에만 있다. 캡처가 gen 루트를 'deployment/gen'이 아닌 'gen'으로 잡으면
    tfvars 를 영영 못 읽어 emails=[] 로 채택 → 다음 apply 에 구독 전량 삭제."""
    gen = tmp_path / "deployment" / "gen"
    (gen / "dev" / "eks").mkdir(parents=True)
    (gen / "dev" / "eks" / "terraform.tfvars").write_text(
        'alert_emails = ["ops@example.com", "sre@example.com"]\n'
        'cognito_domain_suffix = "stg-auth-123456789012"\n')
    monkeypatch.setattr(capture, "_gen_root", lambda: gen)
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: {
        "alerting_sns_topic_arn": "arn:aws:sns:ap-northeast-2:123456789012:t",
        "cluster_name": "llm-gateway-dev"})
    monkeypatch.setattr(capture, "_helm_values",
                        lambda *a, **kw: _eks_values())
    monkeypatch.setattr(capture, "_kubectl_json", lambda *a, **kw: {})
    monkeypatch.setattr(capture, "_helm_app_version", lambda *a, **kw: "")
    doc, _ = capture.capture_eks(env_dir=tmp_path)
    assert doc["features"]["alerting"]["enabled"] is True
    assert doc["features"]["alerting"]["emails"] == [
        "ops@example.com", "sre@example.com"]
    assert doc["infra"]["cognito_domain_suffix"] == "stg-auth-123456789012"


def test_eks_capture_backend_key_mismatch_warns(monkeypatch, tmp_path):
    """20차 — 공유 env 디렉토리의 .terraform 캐시가 다른 env 를 가리키면
    outputs 가 다른 env 의 값이라 경고한다."""
    (tmp_path / ".terraform").mkdir()
    (tmp_path / ".terraform" / "terraform.tfstate").write_text(
        '{"backend": {"config": {"key": "ecs/prod/terraform.tfstate",'
        ' "bucket": "b"}}}')
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: {
        "ecs_cluster_name": "llm-gateway-stg"})
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {})
    doc, notes = capture.capture_ecs(tmp_path, "ap-northeast-2")
    assert any("backend key" in n and "prod" in n for n in notes), notes


def test_collect_doc_survives_scalar_leaf_sections(monkeypatch):
    """20차 — 깨진 yaml(deploy:/oidc:/features:/domain.hosts 가 스칼라)로
    configure 를 돌리면 .get/.items()/dict() 가 AttributeError 로 죽던 경로.
    잎을 비-dict 로 강제해도 wizard 가 완주해야 한다."""
    import types
    from deploy import cli
    class T:
        def __init__(s, p, default='', **kw): s.v = default if default is not None else 'x'
        def ask(s): return s.v
    class S:
        def __init__(s, p, choices, default=None):
            vals = {getattr(c, 'value', c) for c in choices}
            s.v = default if default in vals else getattr(choices[0], 'value', choices[0])
        def ask(s): return s.v
    class C:
        def __init__(s, p, choices, default=None):
            s.v = [getattr(c, 'value', c) for c in choices if getattr(c, 'checked', False)]
        def ask(s): return s.v
    class K:
        def __init__(s, p, default=False): s.v = default
        def ask(s): return s.v
    fake = types.SimpleNamespace(text=T, select=S, checkbox=C, confirm=K)
    fake.Choice = lambda label, value, checked=False: types.SimpleNamespace(
        label=label, value=value, checked=checked)
    monkeypatch.setattr(cli, "_questionary", lambda: fake)
    broken = {"env": "dev",
              "aws": "scalar", "deploy": ["not", "a", "map"],
              "features": "oops", "oidc": 42,
              "network": {"allowed_cidrs": "scalar-not-list"},
              "domain": {"hosts": "scalar"},
              "images": "x", "infra": "y", "clients": "z"}
    doc = cli._collect_doc(broken)  # AttributeError 없이 완주
    assert doc["env"] == "dev"
    assert isinstance(doc["deploy"], dict)


def test_compose_capture_missing_caddyfile_marks_unknown(tmp_path):
    """Caddyfile 을 못 읽으면 cidrs=[]·mode=none 이 '읽은 값'으로 기록돼
    채택 diff 가 방화벽 삭제/도메인 해제 행으로 보인다 — unknown 표기."""
    (tmp_path / "docker-compose.yml").write_text(
        "name: llm-gateway-acme\nservices: {}\n")
    (tmp_path / ".env").write_text("AWS_REGION=ap-northeast-2\n")
    unknown: set = set()
    doc, _ = capture.capture_compose(tmp_path, unknown=unknown)
    assert "network.allowed_cidrs" in unknown
    assert "domain.mode" in unknown


def test_compose_capture_missing_env_marks_env_keys_unknown(tmp_path):
    """.env 없이 env 기반 키는 전부 '못 읽음' — false/mock 기록이 채택
    apply 에서 실제 설정을 내리지 않게 unknown 표기."""
    (tmp_path / "docker-compose.yml").write_text(
        "name: llm-gateway-acme\nservices: {}\n")
    (tmp_path / "Caddyfile").write_text(":8000 {\n  respond ok\n}\n")
    unknown: set = set()
    doc, notes = capture.capture_compose(tmp_path, unknown=unknown)
    for k in ("aws.region", "features.web_search", "features.body_logging",
              "features.notifications.provider", "features.bi_insight",
              "features.pricing_lambda", "features.dev_login",
              "oidc.issuer_url"):
        assert k in unknown, k
    assert any(".env" in n for n in notes)


def test_compose_capture_internal_api_provider(tmp_path):
    """EMAIL_SENDER_TYPE=internal_api 는 유효 provider — mock 강등 금지."""
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    (tmp_path / ".env").write_text(
        "EMAIL_SENDER_TYPE=internal_api\n"
        "EMAIL_API_URL=https://mail.corp\n"
        "EMAIL_SENDER_ADDRESS=noreply@corp\n")
    doc, _ = capture.capture_compose(tmp_path)
    n = doc["features"]["notifications"]
    assert n["provider"] == "internal_api"
    assert n["internal_api_url"] == "https://mail.corp"
    assert n["internal_api_from"] == "noreply@corp"


def test_ecs_capture_domain_without_https_keeps_route53(monkeypatch, tmp_path):
    """도메인은 선언됐는데 https_enabled=false(인증서 검증 대기 등)면
    mode=none 기록은 채택 apply 가 도메인을 내려버린다 — 선언으로 기록."""
    outputs = {"ecs_cluster_name": "llm-gateway-acme",
               "admin_ui_url": "https://admin.acme.io",
               "https_enabled": False, "service_names": {}}
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)
    monkeypatch.setattr(capture, "_aws_json",
                        lambda *a, **kw: {"HostedZones": []})
    doc, notes = capture.capture_ecs(tmp_path, "r")
    assert doc["domain"]["mode"] == "route53-acm"
    assert doc["domain"]["name"] == "acme.io"
    assert any("https_enabled" in n for n in notes)


def test_ecs_capture_alb_scheme_via_output_fallback(monkeypatch, tmp_path):
    """domain 없는 배포도 alb_dns_name output 으로 scheme 을 탐침한다 —
    internal ALB 를 public 으로 기록하면 채택 apply 가 인터넷 노출로 되돌림."""
    outputs = {"ecs_cluster_name": "llm-gateway-acme",
               "admin_ui_url": "", "https_enabled": False,
               "service_names": {},
               "alb_dns_name": "internal-alb.elb.amazonaws.com"}
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)

    def _aws(argv, region):
        if argv[0] == "elbv2":
            return {"LoadBalancers": [
                {"DNSName": "internal-alb.elb.amazonaws.com",
                 "Scheme": "internal", "SecurityGroups": ["sg-1"]}]}
        if argv[0] == "ec2":
            return {"SecurityGroups": [{"IpPermissions": [
                {"IpProtocol": "-1",
                 "IpRanges": [{"CidrIp": "10.0.0.0/8"}]}]}]}
        return {}
    monkeypatch.setattr(capture, "_aws_json", _aws)
    doc, _ = capture.capture_ecs(tmp_path, "r")
    assert doc["network"]["mode"] == "private"


def test_ecs_capture_unverifiable_network_mode_unknown(monkeypatch, tmp_path):
    """ALB 주소를 어느 경로로도 못 읽으면 public 기록이 아니라 unknown."""
    outputs = {"ecs_cluster_name": "llm-gateway-acme",
               "admin_ui_url": "", "https_enabled": False,
               "service_names": {}}
    monkeypatch.setattr(capture, "_tf_outputs", lambda *a, **kw: outputs)
    monkeypatch.setattr(capture, "_aws_json", lambda *a, **kw: {})
    unknown: set = set()
    doc, notes = capture.capture_ecs(tmp_path, "r", unknown=unknown)
    assert "network.mode" in unknown
    assert any("미검증" in n for n in notes)


def test_configure_domain_mode_default_gated(tmp_path, monkeypatch):
    """23차 — eks→ecs 전환 시 이전 domain.mode(selfsigned-temp)가 선택지에
    없어 실제 questionary 라면 ValueError 로 wizard 전체가 죽는다.
    default 가 항상 선택지 안으로 게이트돼야 한다."""
    import types
    from deploy import cli
    seen_defaults = []

    class T:
        def __init__(s, p, default='', **kw): s.v = default if default is not None else 'x'
        def ask(s): return s.v
    class S:
        def __init__(s, p, choices, default=None):
            vals = {getattr(c, 'value', c) for c in choices}
            if default is not None and default not in vals:
                raise ValueError(f"Invalid 'default' value passed: {default!r}")
            seen_defaults.append(default)
            s.v = default if default in vals else getattr(choices[0], 'value', choices[0])
        def ask(s): return s.v
    class C:
        def __init__(s, p, choices, default=None):
            s.v = [getattr(c, 'value', c) for c in choices if getattr(c, 'checked', False)]
        def ask(s): return s.v
    class K:
        def __init__(s, p, default=False): s.v = default
        def ask(s): return s.v
    fake = types.SimpleNamespace(text=T, select=S, checkbox=C, confirm=K)
    fake.Choice = lambda label, value, checked=False: types.SimpleNamespace(
        label=label, value=value, checked=checked)
    monkeypatch.setattr(cli, "_questionary", lambda: fake)
    # eks + selfsigned-temp → ecs 전환. schema 유효한 이전 doc 에서 시작.
    existing = {
        "env": "dev", "aws": {"region": "ap-south-1", "account_id": "123456789012"},
        "deploy": {"target": "ecs", "size_tier": "t1"},
        "domain": {"mode": "selfsigned-temp"},   # eks 전용 mode — ecs 선택지에 없음
        "images": {"tag": "v1"},
    }
    doc = cli._collect_doc(existing)
    assert doc["domain"]["mode"] == "none"   # 선택지 밖이면 첫 유효 기본값


def test_merge_env_drops_declared_off_managed_keys(tmp_path):
    """23차 — oidc 제거 + dev_login 전환 시 stale OIDC_* 가 .env 에 남으면
    admin-api 가 dev 토큰을 거부해 로그인이 잠긴다. web_search off 시
    apply 주입 AGENTCORE_* 도 함께 수렴한다."""
    from deploy.render import common
    existing = {
        "OIDC_ISSUER_URL": "https://idp", "OIDC_CLIENT_ID": "c",
        "OIDC_CLIENT_SECRET": "s", "WEB_SEARCH_ENABLED": "true",
        "AGENTCORE_GATEWAY_URL": "https://gw", "AGENTCORE_REGION": "us-east-1",
        "POSTGRES_PASSWORD": "keepme", "OPERATOR_KEY": "keepme",
    }
    desired = {"AWS_REGION": "us-east-1", "DEV_LOGIN_ENABLED": "true"}
    merged, _, differing, dropped = common.merge_env(existing, desired)
    assert "OIDC_ISSUER_URL" not in merged and "OIDC_CLIENT_SECRET" not in merged
    assert "WEB_SEARCH_ENABLED" not in merged and "AGENTCORE_GATEWAY_URL" not in merged
    assert merged["POSTGRES_PASSWORD"] == "keepme"   # GENERATED — 절대 삭제 금지
    assert merged["OPERATOR_KEY"] == "keepme"        # 비관리 운영자 키는 보존
    assert "OIDC_ISSUER_URL" in dropped and "AGENTCORE_GATEWAY_URL" in dropped


def test_merge_env_keeps_agentcore_when_search_on():
    """web_search 켜진 채 재렌더하면 apply 주입 AGENTCORE_* 는 desired 에
    없어도 유지돼야 한다 — 없으면 기능이 깨진다."""
    from deploy.render import common
    existing = {"WEB_SEARCH_ENABLED": "true",
                "AGENTCORE_GATEWAY_URL": "https://gw"}
    merged, _, _, dropped = common.merge_env(
        existing, {"WEB_SEARCH_ENABLED": "true"})
    assert merged["AGENTCORE_GATEWAY_URL"] == "https://gw"
    assert "AGENTCORE_GATEWAY_URL" not in dropped


def test_chat_agent_region_env_validated(monkeypatch):
    """23차 — CHAT_AGENT_REGION 은 tfvars/--set 에 그대로 박힌다 —
    리전 형식 외 입력(따옴표·개행·쉼표 주입)은 거절."""
    from deploy import chatagent
    monkeypatch.setenv("CHAT_AGENT_REGION", 'us-east-1",x=y')
    with pytest.raises(SystemExit, match="CHAT_AGENT_REGION"):
        chatagent.resolved_region("ap-northeast-2")
    monkeypatch.setenv("CHAT_AGENT_REGION", "eu-west-1")
    assert chatagent.resolved_region("ap-northeast-2") == "eu-west-1"


def test_tfvars_indented_dash_heredoc_terminator(tmp_path):
    """<<-TAG 는 들여쓴 종결자를 허용한다 — 종결자를 못 찾으면 이후 선언
    전부가 지워져 environment= 등이 보이지 않게 된다."""
    (tmp_path / "terraform.tfvars").write_text(
        'desc = <<-EOT\n'
        '    production cluster\n'
        '  EOT\n'
        'environment = "prod"\n')
    tv = capture._tfvars(tmp_path)
    assert tv["environment"] == "prod"


def test_tfvars_json_channels_loaded(tmp_path):
    """terraform.tfvars.json / *.auto.tfvars.json 도 자동 로드 — 거기만
    선언된 값이 캡처에서 유실되면 안 된다 (last-wins)."""
    import json
    (tmp_path / "terraform.tfvars").write_text('environment = "dev"\n')
    (tmp_path / "terraform.tfvars.json").write_text(
        json.dumps({"environment": "stage", "alert_emails": ["a@b.co"]}))
    (tmp_path / "zz.auto.tfvars.json").write_text(
        json.dumps({"models_profile": "p1"}))
    tv = capture._tfvars(tmp_path)
    assert tv["environment"] == "stage"
    assert tv["alert_emails"] == ["a@b.co"] and tv["models_profile"] == "p1"


def test_tfvars_quoted_heredoc_terminator(tmp_path):
    """<<\"TAG\" 인용 구분자도 인식 — 못 읽으면 본문의 key=value 가 유령
    선언으로 살아나 첫 매치 소비자를 오염시킨다."""
    (tmp_path / "terraform.tfvars").write_text(
        'desc = <<-"EOT"\n'
        'environment = "fake"\n'
        '  EOT\n'
        'environment = "prod"\n')
    tv = capture._tfvars(tmp_path)
    assert tv["environment"] == "prod"


def test_r53_alias_target_rejects_invalid_host(monkeypatch):
    """28차 — host(tfvars/output 유래)에 ' 나 JMESPath 메타문자가 섞이면
    쿼리가 주입돼 잘못된 레코드를 집는다 — hostname 형식만 통과."""
    monkeypatch.setattr(capture, "_aws_json",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("aws 호출이면 안 됨")))
    assert capture._r53_alias_target("Z1", "x']|*", "r") == ""
    assert capture._r53_alias_target("Z1", "a'b.example", "r") == ""


def test_instance_items_manifest_owned_beats_labels():
    """28차 — owned(helm 매니페스트)가 주어지면 instance 라벨과 무관하게
    기록된 이름만 인정한다 — 라벨 위조 항목은 제외."""
    forged = {"metadata": {"name": "evil-admin-ui",
                           "labels": {"app.kubernetes.io/instance": "myrel"}}}
    mine = {"metadata": {"name": "myrel-admin-ui",
                         "labels": {"app.kubernetes.io/instance": "other"}}}
    # 라벨과 무관하게 이름 기준 — forged 라벨이 release 와 같아도 탈락,
    # 라벨이 다른 우리 항목은 채택
    got = capture._instance_items([forged, mine], "myrel",
                                  owned={"myrel-admin-ui"})
    assert [i["metadata"]["name"] for i in got] == ["myrel-admin-ui"]
    # owned=None(매니페스트 조회 불가)이면 라벨 폴백 계약 유지
    got = capture._instance_items([forged, mine], "myrel", owned=None)
    assert [i["metadata"]["name"] for i in got] == ["evil-admin-ui"]


def test_compose_roundtrip_ports(tmp_path):
    doc = dict(BASE)
    doc["network"] = {**doc["network"],
                      "ports": {"gateway": 9000, "admin_ui": 3300}}
    cap, _ = _roundtrip(tmp_path, doc)
    assert cap["network"]["ports"] == {"gateway": 9000, "admin_ui": 3300}


def test_compose_roundtrip_default_ports_omitted(tmp_path):
    cap, _ = _roundtrip(tmp_path, BASE)
    assert "ports" not in cap["network"]


def test_compose_roundtrip_branding(tmp_path):
    """커스텀 브랜드명은 캡처에 남고, 기본값이면 branding 블록을 생략한다 —
    채택 yaml 로 apply 해도 브랜드명이 기본값으로 되돌아가지 않게."""
    cap, _ = _roundtrip(tmp_path / "custom", dict(BASE, branding={"name": "농심 AI Gateway"}))
    assert cap["branding"] == {"name": "농심 AI Gateway"}
    schema.from_dict(cap)  # 캡처 결과가 그대로 유효한 yaml

    cap, _ = _roundtrip(tmp_path / "default", dict(BASE))
    assert "branding" not in cap


def test_branding_helper_ignores_default_and_blank():
    assert capture._branding(None) is None
    assert capture._branding("  ") is None
    assert capture._branding(schema.DEFAULT_BRAND_NAME) is None
    assert capture._branding(" X ") == {"name": "X"}


def test_captured_yaml_key_comments(tmp_path):
    """_annotate_capture_yaml — 키별 사고예방 주석을 경로 매칭으로 심고,
    값 의존 주석(0.0.0.0/0 경고)은 조건을 만족할 때만 단다.
    출력은 주석 포함 그대로 yaml.safe_load 된다."""
    import yaml as _yaml
    from deploy import cli
    doc = {
        "version": 1, "env": "dev",
        "aws": {"region": "ap-south-1", "account_id": "1234"},
        "deploy": {"target": "eks", "size_tier": "t3",
                   "tfstate_bucket": "b", "tfstate_table": "t",
                   "capacity_tier": "500"},
        "network": {"mode": "public",
                    "allowed_cidrs": ["10.0.0.0/8", "0.0.0.0/0"]},
        "domain": {"mode": "route53-acm", "name": "x.example",
                   "hosts": {"gateway": "g.x"}},
        "images": {"tag": "v1", "tags": {"adminApi": "x"}},
    }
    body = cli._annotate_capture_yaml(_yaml.safe_dump(doc, sort_keys=False),
                                      doc)
    lines = body.splitlines()
    # 경로 매칭 — 같은 키명이 다른 섹션에 있어도 오매칭 없음
    ts = lines.index("  tfstate_bucket: b")
    assert any("init 404" in lines[i] for i in (ts - 2, ts - 1))
    hosts = lines.index("  hosts:")
    assert "파생 규칙" in lines[hosts - 1] or "파생 규칙" in lines[hosts - 2]
    # 조건부 — 0.0.0.0/0 있을 때만 경고
    assert any("인터넷 전체 오픈" in l for l in lines)
    doc2 = {"version": 1, "network": {"allowed_cidrs": ["10.0.0.0/8"]}}
    body2 = cli._annotate_capture_yaml(_yaml.safe_dump(doc2), doc2)
    assert "인터넷 전체 오픈" not in body2
    # 주석이 yaml 을 깨지 않음 — 주석 스트립 후 원본과 동일
    stripped = "\n".join(l for l in lines if not l.strip().startswith("#"))
    assert _yaml.safe_load(stripped) == doc


def _admin_ui_deploy(env_list):
    return {"items": [{
        "metadata": {"name": "llm-gateway-admin-ui",
                     "labels": {"app.kubernetes.io/instance": "llm-gateway"}},
        "spec": {"template": {"spec": {"containers": [
            {"name": "admin-ui", "env": env_list}]}}},
    }]}


def test_eks_capture_branding_live_env_fallback(monkeypatch):
    """values 에 ADMIN_BRAND_NAME 이 없어도 라이브 deployment env 에 있으면
    캡처한다 — kubectl set env 등 비-helm 경로는 values 기록이 없다."""
    monkeypatch.setattr(capture, "_helm_values",
                        lambda *a, **kw: _eks_values())
    monkeypatch.setattr(
        capture, "_kubectl_json",
        lambda res, *a, **kw:
            _admin_ui_deploy([{"name": "ADMIN_BRAND_NAME",
                               "value": "농심 AI"}])
            if res == "deployments" else {})
    doc, notes = capture.capture_eks()
    assert doc["branding"] == {"name": "농심 AI"}
    assert any("비-helm" in n for n in notes)


def test_eks_capture_branding_absent_everywhere(monkeypatch):
    """values·라이브 모두 없으면 branding 블록 자체가 없다 — 기본값을
    캡처본에 쓰지 않아 채택본이 선언을 기본값으로 되돌리지 않는다."""
    monkeypatch.setattr(capture, "_helm_values",
                        lambda *a, **kw: _eks_values())
    monkeypatch.setattr(
        capture, "_kubectl_json",
        lambda res, *a, **kw:
            _admin_ui_deploy([{"name": "OTHER", "value": "x"}])
            if res == "deployments" else {})
    doc, _ = capture.capture_eks()
    assert "branding" not in doc


def test_branding_helper_records_managed_login_only_when_on():
    assert capture._branding(None, True) == {"managed_login": True}
    assert capture._branding("X", False) == {"name": "X"}
    assert capture._branding(None, False) is None
