# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks_apply — helm/kubectl 호출 경계 단위 테스트."""
import json
import subprocess
import types

import pytest

from deploy import eks_apply


def _cp(items):
    return subprocess.CompletedProcess([], 0, json.dumps({"items": items}), "")


def _ingress(name, hostname=None):
    item = {"metadata": {"name": name}, "status": {}}
    if hostname:
        item["status"] = {"loadBalancer": {"ingress": [{"hostname": hostname}]}}
    return item


def test_live_alb_hostname_picks_admin_ui_not_first(monkeypatch):
    """kubectl 리스트는 이름순이라 -admin-api 가 먼저 온다. 첫 hostname 을 집으면
    NEXTAUTH_URL 이 admin-api ALB 를 가리켜 OIDC 콜백이 깨진다 — 이름으로 골라야 한다."""
    items = [
        _ingress("llm-gateway-admin-api", "admin-api-123.elb.amazonaws.com"),
        _ingress("llm-gateway-admin-ui", "admin-ui-456.elb.amazonaws.com"),
        _ingress("llm-gateway-gateway", "gw-789.elb.amazonaws.com"),
    ]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    assert (eks_apply.live_alb_hostname({"namespace": "ns"})
            == "admin-ui-456.elb.amazonaws.com")


def test_live_alb_hostname_custom_release_suffix(monkeypatch):
    # fullnameOverride 로 이름이 달라도 -admin-ui 접미사는 유지된다
    items = [_ingress("custom-admin-ui", "ui.elb.amazonaws.com")]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    assert eks_apply.live_alb_hostname({"namespace": "ns"}) == "ui.elb.amazonaws.com"


def test_live_alb_hostname_skips_admin_ui_without_ip(monkeypatch):
    # admin-ui 의 ALB 가 아직 안 떴는데 다른 것만 뜬 상태 → "" (재시도 유도)
    items = [
        _ingress("llm-gateway-admin-api", "admin-api.elb.amazonaws.com"),
        _ingress("llm-gateway-admin-ui"),   # hostname 아직 없음
        _ingress("llm-gateway-gateway", "gw.elb.amazonaws.com"),
    ]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    assert eks_apply.live_alb_hostname({"namespace": "ns"}) == ""


def test_live_alb_hostname_kubectl_failure(monkeypatch):
    r = subprocess.CompletedProcess([], 1, "", "connection refused")
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: r)
    assert eks_apply.live_alb_hostname({"namespace": "ns"}) == ""


def test_live_alb_hostname_skips_foreign_release(monkeypatch):
    """공유 ns 의 다른 릴리스 admin-ui ingress 를 집으면 2-phase 가 외부
    ALB 를 가리킨다 — instance 라벨(없으면 정확한 이름)로 우리 것만 고른다."""
    foreign = _ingress("other-admin-ui", "other-ui.elb.amazonaws.com")
    foreign["metadata"]["labels"] = {"app.kubernetes.io/instance": "other"}
    mine = _ingress("myrel-admin-ui", "my-ui.elb.amazonaws.com")
    mine["metadata"]["labels"] = {"app.kubernetes.io/instance": "myrel"}
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda *a, **k: _cp([foreign, mine]))
    meta = {"namespace": "ns", "release": "myrel"}
    assert eks_apply.live_alb_hostname(meta) == "my-ui.elb.amazonaws.com"


def test_live_ingress_rule_host_skips_foreign_release(monkeypatch):
    """외부 릴리스의 host 가 있으면 2-phase 가 스킵돼 우리 nextauthUrl 이
    영영 미설정 — 라벨 불일치 항목은 무시해야 한다."""
    foreign = {"metadata": {"name": "other-admin-ui",
                            "labels": {"app.kubernetes.io/instance": "other"}},
               "spec": {"rules": [{"host": "other.example.com"}]}}
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda *a, **k: _cp([foreign]))
    meta = {"namespace": "ns", "release": "myrel"}
    assert eks_apply.live_ingress_rule_host(meta) == ""


def test_live_alb_hostname_labeled_own_ingresses_still_pick_admin_ui(
        monkeypatch):
    """차트는 instance 라벨을 3개 ingress 전부에 찍는다 — 라벨만 보고
    소유 확인하면 이름 정렬 첫 항목 admin-api 가 선택된다 (13차 회귀)."""
    def _labeled(name, host):
        it = _ingress(name, host)
        it["metadata"]["labels"] = {"app.kubernetes.io/instance": "myrel"}
        return it
    items = [
        _labeled("myrel-admin-api", "api.elb.amazonaws.com"),
        _labeled("myrel-admin-ui", "ui.elb.amazonaws.com"),
        _labeled("myrel-gateway", "gw.elb.amazonaws.com"),
    ]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    assert (eks_apply.live_alb_hostname({"namespace": "ns", "release": "myrel"})
            == "ui.elb.amazonaws.com")


def test_alb_hostname_by_suffix_labeled_routes_each_host(monkeypatch):
    """ensure_domain_records 가 suffix 별로 호출 — 라벨 통과 후에도 이름으로
    구분해야 gateway/admin-ui/admin-api 가 각자의 ALB 를 받는다."""
    def _labeled(name, host):
        it = _ingress(name, host)
        it["metadata"]["labels"] = {"app.kubernetes.io/instance": "myrel"}
        return it
    items = [
        _labeled("myrel-admin-api", "api.elb.amazonaws.com"),
        _labeled("myrel-admin-ui", "ui.elb.amazonaws.com"),
        _labeled("myrel-gateway", "gw.elb.amazonaws.com"),
    ]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    meta = {"namespace": "ns", "release": "myrel"}
    assert (eks_apply._alb_hostname_by_suffix(meta, "-gateway")
            == "gw.elb.amazonaws.com")
    assert (eks_apply._alb_hostname_by_suffix(meta, "-admin-ui")
            == "ui.elb.amazonaws.com")
    assert (eks_apply._alb_hostname_by_suffix(meta, "-admin-api")
            == "api.elb.amazonaws.com")


def test_live_ingress_rule_host_labeled_picks_admin_ui(monkeypatch):
    """라벨 통과 후에도 admin-ui 이름 검사 — admin-api 의 host 를 집으면
    2-phase 가 잘못 스킵된다."""
    items = [
        {"metadata": {"name": "myrel-admin-api",
                      "labels": {"app.kubernetes.io/instance": "myrel"}},
         "spec": {"rules": [{"host": "api.example.com"}]}},
        {"metadata": {"name": "myrel-admin-ui",
                      "labels": {"app.kubernetes.io/instance": "myrel"}},
         "spec": {"rules": [{"host": "ui.example.com"}]}},
    ]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    meta = {"namespace": "ns", "release": "myrel"}
    assert eks_apply.live_ingress_rule_host(meta) == "ui.example.com"


def test_context_flag_propagates(monkeypatch):
    seen = {}
    def fake(argv, **k):
        seen["argv"] = argv
        return _cp([])
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    eks_apply.live_alb_hostname({"namespace": "ns"}, context="prod-ctx")
    assert "--context" in seen["argv"] and "prod-ctx" in seen["argv"]


def _cfg(**over):
    from deploy import schema
    doc = {
        "version": 1, "env": "dev",
        "aws": {"region": "ap-northeast-2"},
        "deploy": {"target": "eks", "size_tier": "t3"},
        "features": {"notifications": {"provider": "mock"}},
        "images": {"tag": "v1"},
    }
    doc.update(over)
    return schema.from_dict(doc)


def test_ses_notification_wires_worker_irsa(monkeypatch, capsys):
    """install-eks.sh R24-S7 계약 — provider=ses 이면 notification-worker SA 에
    SES IRSA 가 붙어야 한다. 없으면 발송이 전량 실패."""
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    cfg = _cfg(features={"notifications": {"provider": "ses", "ses_from": "a@b.c"}})
    outs = {"notification_worker_ses_role_arn": "arn:aws:iam::1:role/ses"}
    args = eks_apply.dynamic_set_args(cfg, outs, "ap-northeast-2")
    joined = " ".join(args)
    assert "notificationWorker.serviceAccount" in joined
    assert "arn:aws:iam::1:role/ses" in joined


def test_ses_notification_missing_role_warns(monkeypatch, capsys):
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    cfg = _cfg(features={"notifications": {"provider": "ses", "ses_from": "a@b.c"}})
    args = eks_apply.dynamic_set_args(cfg, {}, "ap-northeast-2")
    assert not any("notificationWorker" in a for a in args)
    assert "notification_worker_ses_role_arn" in capsys.readouterr().err


def test_mock_notification_skips_worker_irsa(monkeypatch):
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    args = eks_apply.dynamic_set_args(_cfg(), {}, "ap-northeast-2")
    assert not any("notificationWorker" in a for a in args)


def test_body_logging_wires_admin_api_bucket_env(monkeypatch):
    """본문 뷰어 — bucket output 은 gateway-proxy(쓰기)와 admin-api(읽기)
    **양쪽** env 로 간다. admin 쪽이 빠지면 뷰어가 503 으로 죽는다."""
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "bkt"}
    args = eks_apply.dynamic_set_args(cfg, outs, "ap-northeast-2")
    joined = " ".join(args)
    assert "gatewayProxy.env.BODY_LOG_S3_BUCKET=bkt" in joined
    assert "adminApi.env.BODY_LOG_S3_BUCKET=bkt" in joined


def test_body_logging_off_no_bucket_env(monkeypatch):
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    args = eks_apply.dynamic_set_args(
        _cfg(), {"body_log_bucket": "bkt"}, "ap-northeast-2")
    assert not any("BODY_LOG_S3_BUCKET" in a for a in args)


def test_dry_run_flag_server_for_helm_313(monkeypatch):
    monkeypatch.setattr(eks_apply, "_helm_version", lambda: (3, 14))
    assert eks_apply._dry_run_flag() == "--dry-run=server"


def test_dry_run_flag_legacy_before_313(monkeypatch):
    monkeypatch.setattr(eks_apply, "_helm_version", lambda: (3, 10))
    assert eks_apply._dry_run_flag() == "--dry-run"


def test_dry_run_flag_server_on_helm4(monkeypatch):
    monkeypatch.setattr(eks_apply, "_helm_version", lambda: (4, 0))
    assert eks_apply._dry_run_flag() == "--dry-run=server"


# ==============================================================================
# ensure_infra — 선언된 기능 인프라의 targeted terraform apply
# ==============================================================================

def _env_dir(tmp_path):
    d = tmp_path / "env"
    d.mkdir()
    return d


def _tf_quiet(monkeypatch, *, display_rc=0, plan_rc=0, resource_changes=None,
              show_rc=0):
    """_run_quiet fake — terraform init/plan(-detailed 표시용 / -out 실제용)/
    show -json 응답. -detailed-exitcode plan 은 display_rc, -out plan 은
    plan_rc 로 구분해 응답한다. 반환값은 호출 argv 누적 리스트."""
    calls = []
    # 실제 -target plan 의 terraform show -json 에는 target 모듈 의존 리소스
    # (eks·cognito·data source)의 no-op/read/빈 actions 항목이 항상 섞여 나온다
    # — 범위 검증이 이를 건너뛰는지까지 같이 검증한다.
    noop_deps = [
        {"mode": "managed", "address": "module.eks.aws_eks_cluster.this[0]",
         "change": {"actions": ["no-op"]}},
        {"mode": "data", "address": "module.irsa.data.aws_caller_identity.current",
         "change": {"actions": ["read"]}},
        {"mode": "managed", "address": "module.cognito.aws_cognito_user_pool.this",
         "change": {"actions": []}},
    ]

    def fake(argv, **k):
        calls.append(argv)
        if "show" in argv:
            return subprocess.CompletedProcess(
                argv, show_rc,
                json.dumps({"resource_changes":
                            noop_deps + (resource_changes or [])}), "")
        if "plan" in argv:
            rc = display_rc if "-detailed-exitcode" in argv else plan_rc
            return subprocess.CompletedProcess(argv, rc, "Plan: 1 to add", "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    return calls


def _managed_change(address, actions=("create",)):
    return {"mode": "managed", "address": address,
            "change": {"actions": list(actions)}}


def test_ensure_infra_targeted_apply_when_body_log_missing(monkeypatch, tmp_path):
    """body_logging=true 인데 output 이 없으면 ops 8-V 와 같은 targeted apply —
    plan -out 파일을 만들고 검증된 그 plan 파일을 그대로 적용한다."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    quiet = _tf_quiet(monkeypatch, resource_changes=[
        _managed_change("module.body_logging.aws_s3_bucket.b[0]")])
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"body_log_firehose_stream": "s",
                                   "body_log_bucket": "b"})
    (tmp_path / "terraform.tfvars").write_text("enable_body_logging = true\n")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), {})
    assert outs["body_log_bucket"] == "b"
    apply_argv = calls[0]  # init 은 _run_quiet — _run 은 apply 뿐
    # 저장된 plan 파일을 적용 — -target/-auto-approve 는 plan 쪽으로 이동했다
    assert apply_argv[:3] == ["terraform", "apply", "-input=false"]
    assert apply_argv[-1].endswith("managed.tfplan")
    plan_argv = [a for a in quiet if "plan" in a][0]
    assert "-out" in plan_argv
    assert "-target=module.body_logging" in plan_argv
    assert "-target=module.irsa" in plan_argv
    assert any(a.startswith("-var-file=") for a in plan_argv)


def test_ensure_infra_skips_when_outputs_present(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "b"}
    assert eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), outs) is outs
    assert calls == []


def test_ensure_infra_ses_targets_irsa_only(monkeypatch, tmp_path):
    """provider=ses 인데 worker IRSA output 이 없으면 module.irsa 만."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    quiet = _tf_quiet(monkeypatch, resource_changes=[
        _managed_change("module.irsa.aws_iam_role.ses")])
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"notification_worker_ses_role_arn": "arn:r"})
    cfg = _cfg(features={"notifications": {"provider": "ses",
                                           "ses_from": "a@b.c"}})
    eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), {})
    plan_argv = [a for a in quiet if "plan" in a][0]
    assert "-target=module.irsa" in plan_argv
    assert not any("body_logging" in a for a in plan_argv)


def test_ensure_infra_noop_when_features_off(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    assert eks_apply.ensure_infra(_cfg(), tmp_path, _env_dir(tmp_path), {}) == {}
    assert calls == []


def test_ensure_infra_missing_env_dir_warns(monkeypatch, tmp_path, capsys):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = eks_apply.ensure_infra(cfg, tmp_path, tmp_path / "nope", {})
    assert outs == {} and calls == []
    assert "terraform env" in capsys.readouterr().err


def test_apply_managed_aborts_on_out_of_scope(monkeypatch, tmp_path):
    """-target 은 의존성(module.eks 등 사람 소유) 변경까지 plan 에 끌어온다 —
    target 모듈 밖 변경이 섞이면 자동 apply 는 중단돼야 한다."""
    import pytest
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    _tf_quiet(monkeypatch, display_rc=2, resource_changes=[
        _managed_change("module.eks.aws_eks_cluster.this[0]", ("update",))])
    (tmp_path / "terraform.tfvars").write_text("enable_body_logging = true\n")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "b"}
    with pytest.raises(SystemExit):
        eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), outs)
    assert not any(c[:2] == ["terraform", "apply"] for c in calls)


def test_apply_managed_aborts_on_destroy(monkeypatch, tmp_path):
    """범위 안이라도 delete/replace 계열이면 자동 적용하지 않는다."""
    import pytest
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: None)
    _tf_quiet(monkeypatch, display_rc=2, resource_changes=[
        _managed_change("module.body_logging.aws_s3_bucket.b[0]",
                        ("delete", "create"))])
    (tmp_path / "terraform.tfvars").write_text("enable_body_logging = true\n")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "b"}
    with pytest.raises(SystemExit):
        eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), outs)


def test_apply_managed_missing_hard_fails_on_plan_error(monkeypatch, tmp_path):
    """출력이 비어 필수인 경로는 plan 자체가 실패하면 배포를 멈춘다."""
    import pytest
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: None)
    _tf_quiet(monkeypatch, plan_rc=1)
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    with pytest.raises(SystemExit):
        eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), {})


def test_apply_managed_missing_no_changes_refreshes_outputs(monkeypatch, tmp_path):
    """리소스는 state 에 있는데 출력만 못 읽은 경우 — plan 변경이 없어도
    terraform output 을 다시 읽어 --set 에 필요한 값을 채운다."""
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: None)
    _tf_quiet(monkeypatch)  # resource_changes=[] → 변경 없음
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"body_log_firehose_stream": "s",
                                   "body_log_bucket": "b"})
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), {})
    assert outs["body_log_bucket"] == "b"


def test_apply_managed_still_missing_aborts(monkeypatch, tmp_path):
    """적용(또는 no-op plan) 후에도 output 이 비면 helm 에 빈 env 가 들어간다 —
    조용히 진행하지 않고 중단한다."""
    import pytest
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: None)
    _tf_quiet(monkeypatch)  # no-op plan
    monkeypatch.setattr(eks_apply, "tf_outputs", lambda d: {})
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    with pytest.raises(SystemExit):
        eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), {})


def test_live_ingress_rule_host_reads_spec_rules(monkeypatch):
    item = _ingress("llm-gateway-admin-ui")
    item["spec"] = {"rules": [{"host": "admin-dev.example.com"}]}
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp([item]))
    assert (eks_apply.live_ingress_rule_host({"namespace": "ns"})
            == "admin-dev.example.com")


def test_live_ingress_rule_host_empty_on_fresh_install(monkeypatch):
    # domain=none 신규 설치 — rule host 없음 → ALB 2-phase 가 필요
    item = _ingress("llm-gateway-admin-ui")
    item["spec"] = {"rules": [{}]}
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp([item]))
    assert eks_apply.live_ingress_rule_host({"namespace": "ns"}) == ""


def _apply_harness(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "tf_outputs", lambda d: {})
    monkeypatch.setattr(eks_apply, "ensure_infra",
                        lambda cfg, gen, env, outs, context="": outs)
    monkeypatch.setattr(eks_apply, "load_meta", lambda g, r: {
        "release": "r", "namespace": "n", "chart": "c",
        "env_dir": "env", "values_layers": []})
    return calls


def test_apply_skips_alb_2phase_when_ingress_host_exists(monkeypatch, tmp_path):
    """채택 배포 — env values 가 admin-dev.example.com host 를 공급하면 차트가
    https://<host> 를 파생한다. ALB 주입을 하면 콜백이 깨지므로 생략해야 한다."""
    calls = _apply_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(eks_apply, "live_ingress_rule_host",
                        lambda m, context="": "admin-dev.example.com")
    monkeypatch.setattr(eks_apply, "live_alb_hostname",
                        lambda m, context="": "alb-123.elb.amazonaws.com")
    eks_apply.apply(_cfg(), tmp_path, tmp_path)
    assert len(calls) == 1  # 2차 upgrade 없음
    assert not any("nextauthUrl" in a for c in calls for a in c)


def test_apply_alb_2phase_on_fresh_install(monkeypatch, tmp_path):
    """domain=none 신규 설치 — rule host 가 없으면 ALB DNS 를 nextauthUrl 로 주입."""
    calls = _apply_harness(monkeypatch, tmp_path)
    monkeypatch.setattr(eks_apply, "live_ingress_rule_host",
                        lambda m, context="": "")
    monkeypatch.setattr(eks_apply, "live_alb_hostname",
                        lambda m, context="": "alb-123.elb.amazonaws.com")
    eks_apply.apply(_cfg(), tmp_path, tmp_path)
    assert len(calls) == 2
    assert "adminUi.nextauthUrl=http://alb-123.elb.amazonaws.com" in calls[1]


def test_strip_es_owned_secrets_removes_only_es_ownerrefs(monkeypatch):
    """ES(Owner)가 만든 Secret 의 ownerRef 만 뗀다 — Orphan/무소유는 건드리지 않음.
    이게 없으면 ES 교체 시 Secret 이 GC 로 삭제돼 전 pod 가 멈춘다(R24-N6r2)."""
    calls = []
    kinds = {"llm-gateway-app": "ExternalSecret",
             "llm-gateway-db": "",
             # llm-gateway-redis 는 없는 시크릿(rc!=0) — 건너뛴다
             }
    def fake(argv, **k):
        calls.append(argv)
        sec = argv[argv.index("secret") + 1] if "secret" in argv else ""
        if "get" in argv:
            return subprocess.CompletedProcess(
                argv, 0 if sec in kinds else 1, kinds.get(sec, ""), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    eks_apply.strip_es_owned_secrets(
        {"namespace": "ns", "release": "llm-gateway"})
    patches = [c for c in calls if "patch" in c]
    assert len(patches) == 1
    assert "llm-gateway-app" in patches[0]
    assert "ownerReferences" in " ".join(patches[0])


def test_plan_never_runs_terraform_apply(monkeypatch, tmp_path, capsys):
    """plan 은 읽기 전용 — 인프라가 비어 있어도 terraform apply 를 돌리지 않는다."""
    calls, quiet = [], []

    def fake_quiet(argv, **k):
        quiet.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    monkeypatch.setattr(eks_apply, "_run_quiet", fake_quiet)
    monkeypatch.setattr(eks_apply, "tf_outputs", lambda d: {})
    monkeypatch.setattr(eks_apply, "load_meta", lambda g, r: {
        "release": "r", "namespace": "n", "chart": "c",
        "env_dir": "env", "values_layers": []})
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    eks_apply.plan(cfg, tmp_path, tmp_path)
    helm_dry = [a for a in quiet if a[:2] == ["helm", "upgrade"]]
    assert helm_dry and ("--dry-run" in helm_dry[0]
                         or "--dry-run=server" in helm_dry[0])
    assert not any("apply" in a for c in calls for a in c)
    assert not any(c[:2] == ["terraform", "apply"] for c in calls + quiet)
    assert "targeted terraform apply" in capsys.readouterr().err


def test_infra_targets_covers_declared_model_and_mantle_keys():
    """managed overlay 소유 키(ARN/mantle)는 module.irsa 정책을 만든다 —
    선언하면 irsa 가 수렴 대상에 들어가야 한다."""
    cfg = _cfg(infra={
        "bedrock_allowed_model_arns": [
            "arn:aws:bedrock:*::foundation-model/openai.*"],
        "mantle_regions": ["us-east-1"]})
    assert "-target=module.irsa" in eks_apply._infra_targets(cfg)


def test_ensure_infra_converges_managed_drift(monkeypatch, tmp_path):
    """output 이 있어도 선언 키가 live 와 어긋나면(표시용 rc=2) 저장 plan 을
    검증하고 그대로 적용해 수렴한다."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    quiet = _tf_quiet(monkeypatch, display_rc=2, resource_changes=[
        _managed_change("module.irsa.aws_iam_policy.bedrock", ("update",))])
    monkeypatch.setattr(eks_apply, "tf_outputs", lambda d: {
        "body_log_firehose_stream": "s", "body_log_bucket": "b"})
    (tmp_path / "terraform.tfvars").write_text("enable_body_logging = true\n")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "b"}
    new_outs = eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), outs)
    assert new_outs is not outs  # 적용 후 terraform output 재조회
    display_plan = [a for a in quiet if "-detailed-exitcode" in a][0]
    assert any(a.startswith("-var-file=") for a in display_plan)
    out_plan = [a for a in quiet if "-out" in a][0]
    assert "-target=module.irsa" in out_plan
    apply_argv = [c for c in calls if c[:2] == ["terraform", "apply"]][0]
    assert apply_argv[-1].endswith("managed.tfplan")
    # -target/-auto-approve 는 plan 생성 시에만 — 저장 plan 은 그대로 적용한다
    assert not any(a.startswith("-target=") for a in apply_argv)


def test_ensure_infra_skips_apply_when_converged(monkeypatch, tmp_path):
    """표시용 plan rc=0 (drift 없음) 이면 -out plan/apply 를 돌리지 않는다."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    quiet = _tf_quiet(monkeypatch)  # plan rc=0
    (tmp_path / "terraform.tfvars").write_text("enable_body_logging = true\n")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "b"}
    assert eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), outs) is outs
    assert calls == []
    assert not any("-out" in a for a in quiet)


def test_ensure_infra_skips_apply_on_plan_failure(monkeypatch, tmp_path, capsys):
    """plan 이 실패(rc!=2,!=0) 하면 drift 여부를 모르니 apply 는 위험 — 건너뛴다."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    _tf_quiet(monkeypatch, display_rc=1)
    (tmp_path / "terraform.tfvars").write_text("enable_body_logging = true\n")
    cfg = _cfg(features={"notifications": {"provider": "mock"},
                         "body_logging": True})
    outs = {"body_log_firehose_stream": "s", "body_log_bucket": "b"}
    assert eks_apply.ensure_infra(cfg, tmp_path, _env_dir(tmp_path), outs) is outs
    assert calls == []
    assert "plan 실패" in capsys.readouterr().err


def test_infra_targets_alerting_module():
    cfg = _cfg(features={"alerting": {"enabled": True}})
    assert "-target=module.alerting" in eks_apply._infra_targets(cfg)


def test_infra_missing_alerting_output():
    cfg = _cfg(features={"alerting": {"enabled": True}})
    assert eks_apply._infra_missing(cfg, {}) == ["alerting(SNS topic/alarms)"]
    assert eks_apply._infra_missing(
        cfg, {"alerting_sns_topic_arn": "arn:aws:sns:ap-northeast-2:1:t"}) == []


# ==============================================================================
# ALB dimension 라이브 발견 — alerting.enabled 일 때 apply 가 ingress→elbv2 를
# 조회해 gen var-file 로 module.alerting 에 주입한다 (첫 배포는 수렴형).
# ==============================================================================

_ARN_LB = ("arn:aws:elasticloadbalancing:ap-south-1:123456789012:"
           "loadbalancer/app/k8s-lb-a/aaa111")
_ARN_TG = ("arn:aws:elasticloadbalancing:ap-south-1:123456789012:"
           "targetgroup/k8s-tg-a/bbb222")


def _alert_cfg():
    return _cfg(features={"notifications": {"provider": "mock"},
                          "alerting": {"enabled": True}})


def _discover_fake(monkeypatch, *, kubectl_rc=0,
                   items=None, lbs=None, tgs=None):
    if items is None:
        items = [{"metadata": {"name": "llm-gateway-gateway",
                               "labels": {"app.kubernetes.io/instance":
                                          "llm-gateway"}},
                  "status": {"loadBalancer":
                             {"ingress": [{"hostname": "alb.example.com"}]}}}]
    """kubectl ingress + aws elbv2 응답 fake — 호출 argv 순서로 분기."""
    calls = []

    def fake(argv, **k):
        calls.append(argv)
        if argv[0] == "kubectl":
            return subprocess.CompletedProcess(argv, kubectl_rc,
                                               json.dumps({"items": items}), "")
        if "describe-load-balancers" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"LoadBalancers": lbs or []}), "")
        if "describe-target-groups" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"TargetGroups": tgs or []}), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    eks_apply._ALB_DIMS_CACHE.clear()  # 프로세스 캐시 — 테스트 간 오염 방지
    eks_apply._WARNED.clear()  # 경고 dedupe 도 동일
    return calls


def test_alb_dims_disabled_no_calls(monkeypatch):
    calls = _discover_fake(monkeypatch)
    cfg = _cfg(features={"notifications": {"provider": "mock"}})
    assert eks_apply._discover_alb_dims(cfg)[:2] == ([], [])
    assert calls == []


def test_alb_dims_kubectl_failure_converges_empty(monkeypatch):
    """첫 배포/kubectl 실패 — 빈 결과로 수렴, 예외 없음."""
    _discover_fake(monkeypatch, kubectl_rc=1)
    assert eks_apply._discover_alb_dims(_alert_cfg())[:2] == ([], [])


def test_alb_dims_no_ingress_hostname_empty(monkeypatch):
    calls = _discover_fake(monkeypatch, items=[])
    assert eks_apply._discover_alb_dims(_alert_cfg())[:2] == ([], [])
    assert not any("describe-load-balancers" in a for a in calls)


def test_alb_dims_resolves_lb_and_tg(monkeypatch):
    lbs = [{"DNSName": "alb.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB},
           {"DNSName": "other.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB.replace("aaa111", "zzz999")},
           {"DNSName": "alb.example.com", "Type": "network",
            "LoadBalancerArn": _ARN_LB + "-nlb"}]
    tgs = [{"TargetGroupArn": _ARN_TG}]
    calls = _discover_fake(monkeypatch, lbs=lbs, tgs=tgs)
    lb_dims, tg_pairs, _ = eks_apply._discover_alb_dims(_alert_cfg())
    # hostname 일치 + application 타입만 — NLB/다른 ALB 제외
    assert lb_dims == ["app/k8s-lb-a/aaa111"]
    assert tg_pairs == [{"lb": "app/k8s-lb-a/aaa111",
                         "tg": "targetgroup/k8s-tg-a/bbb222"}]
    # describe-target-groups 는 LB ARN 당 1회 — 발견된 LB 만
    tg_calls = [a for a in calls if "describe-target-groups" in a]
    assert len(tg_calls) == 1
    assert _ARN_LB in tg_calls[0]


def test_alerting_var_args_writes_tfvars(monkeypatch, tmp_path):
    lbs = [{"DNSName": "alb.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB}]
    tgs = [{"TargetGroupArn": _ARN_TG}]
    _discover_fake(monkeypatch, lbs=lbs, tgs=tgs)
    args, fresh = eks_apply._alerting_var_args(_alert_cfg(), tmp_path)
    f = tmp_path / "alerts-dims.tfvars"
    assert args == [f"-var-file={f.resolve()}"]
    assert fresh is True
    body = f.read_text()
    assert '"app/k8s-lb-a/aaa111"' in body
    assert '"lb": "app/k8s-lb-a/aaa111"' in body
    assert '"tg": "targetgroup/k8s-tg-a/bbb222"' in body


def test_alerting_var_args_empty_no_file(monkeypatch, tmp_path, capsys):
    """ALB 미발견(첫 배포) — var-file 미생성 + 수렴 안내. 다음 apply 가 채운다."""
    _discover_fake(monkeypatch, kubectl_rc=1)
    args, fresh = eks_apply._alerting_var_args(_alert_cfg(), tmp_path)
    assert args == [] and fresh is False
    assert not (tmp_path / "alerts-dims.tfvars").exists()
    err = capsys.readouterr().err
    assert "deploy apply` 를 한 번 더" in err
    assert "알람은 아직 생성되지 않습니다" in err


def test_ensure_infra_alerting_injects_dims(monkeypatch, tmp_path):
    """alerting target plan 에 발견 var-file 이 붙는다 — 수동 편집 없이 관리."""
    calls = []
    monkeypatch.setattr(eks_apply, "_run", lambda argv, **k: calls.append(argv))
    lbs = [{"DNSName": "alb.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB}]
    tf_calls = []
    discover = _discover_fake(monkeypatch, lbs=lbs,
                              tgs=[{"TargetGroupArn": _ARN_TG}])
    # terraform init/plan/show 도 같은 _run_quiet 을 쓰므로 분기 추가 —
    # discover fake 는 교체 후엔 호출되지 않으므로 직접 재구현한다
    lbs_json = json.dumps({"LoadBalancers": lbs})
    tgs_json = json.dumps({"TargetGroups": [{"TargetGroupArn": _ARN_TG}]})

    def fake2(argv, **k):
        if argv[0] == "terraform":
            tf_calls.append(argv)
            if "show" in argv:
                return subprocess.CompletedProcess(
                    argv, 0, json.dumps({"resource_changes": [
                        _managed_change(
                            "module.alerting[0].aws_sns_topic.alerts")]}), "")
            if "plan" in argv:
                rc = 2 if "-detailed-exitcode" in argv else 0
                return subprocess.CompletedProcess(argv, rc, "Plan", "")
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "describe-load-balancers" in argv:
            return subprocess.CompletedProcess(argv, 0, lbs_json, "")
        if "describe-target-groups" in argv:
            return subprocess.CompletedProcess(argv, 0, tgs_json, "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"items": [{"metadata": {"name": "llm-gateway-gateway", "labels": {"app.kubernetes.io/instance": "llm-gateway"}}, "status": {"loadBalancer": {"ingress": [{"hostname": "alb.example.com"}]}}}]}), "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake2)
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"alerting_sns_topic_arn": "arn:t"})
    (tmp_path / "terraform.tfvars").write_text("enable_alerting = true\n")
    eks_apply.ensure_infra(_alert_cfg(), tmp_path, _env_dir(tmp_path), {})
    plan_argv = [a for a in tf_calls if "plan" in a][0]
    assert "-target=module.alerting" in plan_argv
    assert any("alerts-dims.tfvars" in a for a in plan_argv)


def test_alb_dims_cached_per_run(monkeypatch):
    """한 프로세스에서 (ns, region, context) 당 1회만 AWS 호출 — apply 의
    ensure_infra + managed plan 중복 발견 방지."""
    eks_apply._ALB_DIMS_CACHE.clear()
    calls = _discover_fake(monkeypatch, kubectl_rc=1)
    eks_apply._discover_alb_dims(_alert_cfg())
    eks_apply._discover_alb_dims(_alert_cfg())
    assert len(calls) == 1
    eks_apply._ALB_DIMS_CACHE.clear()


def test_alerting_var_args_stale_reuse(monkeypatch, tmp_path, capsys):
    """알람이 있는 상태에서 발견 실패 → 이전 var-file 재사용. 안 하면
    alb_*=[] 평가로 기존 알람이 destroy-plan → 파괴 가드가 apply 중단."""
    eks_apply._ALB_DIMS_CACHE.clear()
    _discover_fake(monkeypatch, kubectl_rc=1)
    f = tmp_path / "alerts-dims.tfvars"
    f.write_text('alb_lb_dimensions = ["app/old/1"]\n')
    args, fresh = eks_apply._alerting_var_args(_alert_cfg(), tmp_path)
    assert args == [f"-var-file={f.resolve()}"]
    assert fresh is False  # stale 재사용은 destroy 허용이 아니다
    assert "재사용" in capsys.readouterr().err
    eks_apply._ALB_DIMS_CACHE.clear()


def test_alb_dims_malformed_arn_skipped(monkeypatch):
    """구분자 없는 ARN·키 부재 항목은 스킵 — nonsense dimension 주입 금지."""
    eks_apply._ALB_DIMS_CACHE.clear()
    lbs = [{"DNSName": "alb.example.com", "Type": "application",
            "LoadBalancerArn": "arn:bogus-no-delimiter"},
           {"DNSName": "alb.example.com", "Type": "application"},  # arn 키 없음
           {"DNSName": "alb.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB}]
    _discover_fake(monkeypatch, lbs=lbs,
                   tgs=[{"TargetGroupArn": "arn:broken"}, {"no-arn": 1},
                        {"TargetGroupArn": _ARN_TG}])
    lb_dims, tg_pairs, _ = eks_apply._discover_alb_dims(_alert_cfg())
    assert lb_dims == ["app/k8s-lb-a/aaa111"]
    assert tg_pairs == [{"lb": "app/k8s-lb-a/aaa111",
                         "tg": "targetgroup/k8s-tg-a/bbb222"}]
    eks_apply._ALB_DIMS_CACHE.clear()


# ==============================================================================
# ALB 알람 destroy 화이트리스트 — dimension 교체는 정상 수명주기
# ==============================================================================

def test_managed_plan_allows_alb_alarm_destroy(tmp_path, monkeypatch):
    """ALB id 변경 → 신 dims 발견 → plan 에 구 알람 delete. 이 리소스는
    허용 — 안 하면 ALB 교체 때마다 apply 가 영구 블록."""
    # 모듈이 count 라 실제 plan 주소는 module.alerting[0].... — [0] 없는
    # 주소로 테스트하면 화이트리스트가 실전에서 발화 안 해도 통과한다
    (tmp_path / "plan.json").write_text(json.dumps({"resource_changes": [
        _managed_change("module.alerting[0].aws_cloudwatch_metric_alarm."
                        "alb_target_5xx[\"app/old/1\"]", ["delete"]),
        _managed_change("module.alerting[0].aws_cloudwatch_metric_alarm."
                        "alb_unhealthy[\"targetgroup/x/1\"]", ["delete"]),
        _managed_change("module.alerting[0].aws_cloudwatch_metric_alarm."
                        "alb_target_5xx[\"app/new/2\"]", ["create"]),
    ]}))
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda a, **k: subprocess.CompletedProcess(
                            a, 0, (tmp_path / "plan.json").read_text(), ""))
    bad, changes, _ = eks_apply._inspect_managed_plan(
        tmp_path, tmp_path / "x.tfplan", ["module.alerting"],
        allow_alb_destroy=True)
    assert bad == []
    assert len(changes) == 3


def test_managed_plan_still_blocks_other_alarm_destroy(tmp_path, monkeypatch):
    """화이트리스트 외 리소스의 destroy 는 여전히 차단 — SNS topic 은
    어느 목록에도 없다."""
    (tmp_path / "plan.json").write_text(json.dumps({"resource_changes": [
        _managed_change("module.alerting[0].aws_sns_topic.alerts",
                        ["delete"]),
    ]}))
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda a, **k: subprocess.CompletedProcess(
                            a, 0, (tmp_path / "plan.json").read_text(), ""))
    bad, _, _ = eks_apply._inspect_managed_plan(
        tmp_path, tmp_path / "x.tfplan", ["module.alerting"])
    assert len(bad) == 1 and "파괴적 변경" in bad[0]


def test_managed_plan_allows_member_alarm_shrink(tmp_path, monkeypatch):
    """M2 — aurora/cache 알람은 모듈 output(member id) 축소를 추종하는
    delete 라 허용. env terraform 의 토폴로지 축소를 뒤따르는 정상 수렴."""
    (tmp_path / "plan.json").write_text(json.dumps({"resource_changes": [
        _managed_change("module.alerting[0].aws_cloudwatch_metric_alarm."
                        "cache_cpu[\"c1\"]", ["delete"]),
        _managed_change("module.alerting[0].aws_cloudwatch_metric_alarm."
                        "aurora_cpu[\"i1\"]", ["delete"]),
    ]}))
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda a, **k: subprocess.CompletedProcess(
                            a, 0, (tmp_path / "plan.json").read_text(), ""))
    bad, changes, _ = eks_apply._inspect_managed_plan(
        tmp_path, tmp_path / "x.tfplan", ["module.alerting"])
    assert bad == [] and len(changes) == 2


def test_doctor_drift_includes_alb_dims_file(tmp_path):
    """doctor drift plan 이 alerts-dims.tfvars 를 넘긴다 — 빠뜨리면
    존재하는 ALB 알람이 매번 destroy drift 로 오진."""
    from deploy import doctor
    eks = tmp_path / "eks"
    eks.mkdir()
    (eks / "terraform.tfvars").write_text("a=1\n")
    (eks / "alerts-dims.tfvars").write_text('alb_lb_dimensions=["x"]\n')
    argv = doctor._tf_drift_plan_argv(tmp_path)
    var_files = [a for a in argv if a.startswith("-var-file=")]
    assert any("terraform.tfvars" in v for v in var_files)
    assert any("alerts-dims.tfvars" in v for v in var_files)


def test_alb_dims_tg_describe_failure_is_whole_failure(monkeypatch):
    """TG 조회 실패는 부분이 아니라 전체 실패 — 부분 dims 로 쓰면 TG 알람이
    destroy-plan 에 잡힌다. 실패 → stale 재사용 경로로 보낸다."""
    eks_apply._ALB_DIMS_CACHE.clear()

    def fake(argv, **k):
        if "describe-target-groups" in argv:
            return subprocess.CompletedProcess(argv, 1, "", "throttled")
        if "describe-load-balancers" in argv:
            return subprocess.CompletedProcess(argv, 0, json.dumps(
                {"LoadBalancers": [{"DNSName": "alb.example.com",
                                    "Type": "application",
                                    "LoadBalancerArn": _ARN_LB}]}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"items": [{"metadata": {"name": "llm-gateway-gateway", "labels": {"app.kubernetes.io/instance": "llm-gateway"}}, "status": {"loadBalancer": {"ingress": [{"hostname": "alb.example.com"}]}}}]}), "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    lb_dims, tg_pairs, note = eks_apply._discover_alb_dims(_alert_cfg())
    assert (lb_dims, tg_pairs) == ([], [])
    assert "describe-target-groups" in note
    eks_apply._ALB_DIMS_CACHE.clear()


def test_managed_plan_blocks_alb_destroy_when_discovery_failed(tmp_path, monkeypatch):
    """F1 회귀 — 발견 실패(allow_alb_destroy=False) 상태의 alb 알람
    delete 는 차단된다. 파일 부재+기존 알람 존재 시 전량 삭제가 자동
    적용되는 것을 막는다."""
    (tmp_path / "plan.json").write_text(json.dumps({"resource_changes": [
        _managed_change("module.alerting[0].aws_cloudwatch_metric_alarm."
                        "alb_target_5xx[\"app/old/1\"]", ["delete"]),
    ]}))
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda a, **k: subprocess.CompletedProcess(
                            a, 0, (tmp_path / "plan.json").read_text(), ""))
    bad, _, blocked = eks_apply._inspect_managed_plan(
        tmp_path, tmp_path / "x.tfplan", ["module.alerting"],
        allow_alb_destroy=False)
    assert bad == []          # bad 가 아니라 soft-blocked 목록으로 분류
    assert len(blocked) == 1 and "차단" in blocked[0]


def test_managed_plan_allows_email_sub_delete(tmp_path, monkeypatch):
    """yaml emails 축소는 선언된 의도 — 구독 delete 는 허용 (F3 회귀)."""
    (tmp_path / "plan.json").write_text(json.dumps({"resource_changes": [
        _managed_change("module.alerting[0].aws_sns_topic_subscription."
                        "email[\"gone@x.com\"]", ["delete"]),
    ]}))
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda a, **k: subprocess.CompletedProcess(
                            a, 0, (tmp_path / "plan.json").read_text(), ""))
    bad, changes, _ = eks_apply._inspect_managed_plan(
        tmp_path, tmp_path / "x.tfplan", ["module.alerting"])
    assert bad == [] and len(changes) == 1


def test_alb_dims_filters_foreign_ingress(monkeypatch):
    """F4+M4 — instance 라벨이 release 와 다른 ingress 는 제외. 접미사가
    같아도(-gateway) 라벨이 다르면 외부 ALB 를 채택하지 않는다."""
    eks_apply._ALB_DIMS_CACHE.clear()
    lbs = [{"DNSName": "ours.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB},
           {"DNSName": "foreign.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB.replace("aaa111", "ccc333")}]
    items = [
        {"metadata": {"name": "llm-gateway-gateway",
                      "labels": {"app.kubernetes.io/instance": "llm-gateway"}},
         "status": {"loadBalancer":
                    {"ingress": [{"hostname": "ours.example.com"}]}}},
        # 접미사는 같지만 다른 release 의 외부 ingress — 제외돼야 한다
        {"metadata": {"name": "payment-gateway",
                      "labels": {"app.kubernetes.io/instance": "payment"}},
         "status": {"loadBalancer":
                    {"ingress": [{"hostname": "foreign.example.com"}]}}},
        # 라벨 없는 레거시 ingress — 제외
        {"metadata": {"name": "legacy-admin-ui"},
         "status": {"loadBalancer":
                    {"ingress": [{"hostname": "foreign.example.com"}]}}},
    ]

    def fake(argv, **k):
        if argv[0] == "kubectl":
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"items": items}), "")
        if "describe-load-balancers" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps({"LoadBalancers": lbs}), "")
        if "describe-target-groups" in argv:
            return subprocess.CompletedProcess(
                argv, 0, json.dumps(
                    {"TargetGroups": [{"TargetGroupArn": _ARN_TG}]}), "")
        return subprocess.CompletedProcess(argv, 0, "", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    lb_dims, tg_pairs, _ = eks_apply._discover_alb_dims(_alert_cfg())
    assert lb_dims == ["app/k8s-lb-a/aaa111"]
    eks_apply._ALB_DIMS_CACHE.clear()


# ==============================================================================
# 5차 리뷰 회귀 — 주석 오인·미매칭 hostname·스칼라 emails
# ==============================================================================

def test_tfvars_list_ignores_commented_and_prefixed(tmp_path):
    """F1 — `# cognito_callback_urls` 주석·legacy_ 접두 변수는 부활 금지.
    죽은 URL 이 OAuth 허용 목록을 오염하는 보안 경로."""
    from deploy.render import eks as eks_render
    (tmp_path / "terraform.tfvars").write_text(
        '# cognito_callback_urls = ["https://old.example.com/cb"]\n'
        'legacy_cognito_callback_urls = ["https://legacy.example.com/cb"]\n'
        'cognito_callback_urls = ["https://live.example.com/cb"]\n'
        '  cognito_logout_urls = ["https://live.example.com/"]\n')
    assert eks_render._tfvars_list(tmp_path, "cognito_callback_urls") == [
        "https://live.example.com/cb"]
    assert eks_render._tfvars_list(tmp_path, "cognito_logout_urls") == [
        "https://live.example.com/"]


def test_manual_alb_pins(tmp_path):
    """F2/F3 + 6차 — 주석 처리된 선언은 수동 고정이 아니고, 변수별로 구분해
    반환한다 (한쪽만 고정이면 다른 패밀리는 여전히 차단돼야 한다)."""
    from deploy import bootstrap
    (tmp_path / "terraform.tfvars").write_text(
        '# alb_lb_dimensions = ["app/x/1"]\nfoo = 1\n')
    assert bootstrap._manual_alb_pins(tmp_path) == set()
    (tmp_path / "terraform.tfvars").write_text(
        'alb_target_groups = [{lb="app/x/1", tg="targetgroup/y/2"}]\n')
    assert bootstrap._manual_alb_pins(tmp_path) == {"alb_target_groups"}
    # terraform 이 실제로 읽는 다른 채널들
    (tmp_path / "terraform.tfvars").write_text("foo = 1\n")
    (tmp_path / "x.auto.tfvars").write_text(
        'alb_lb_dimensions = ["app/x/1"]\n')
    assert bootstrap._manual_alb_pins(tmp_path) == {"alb_lb_dimensions"}
    (tmp_path / "x.auto.tfvars").unlink()
    (tmp_path / "terraform.tfvars.json").write_text(
        '{"alb_target_groups": [{"lb": "a", "tg": "b"}]}')
    assert bootstrap._manual_alb_pins(tmp_path) == {"alb_target_groups"}
    (tmp_path / "terraform.tfvars.json").unlink()
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("TF_VAR_alb_lb_dimensions", '["app/x/1"]')
    assert bootstrap._manual_alb_pins(tmp_path) == {"alb_lb_dimensions"}
    monkeypatch.undo()


def test_apply_managed_blocked_branch(monkeypatch, tmp_path):
    """6차 — _apply_managed 의 blocked 3-튜플 계약을 직접 고정:
    required+blocked → SystemExit, drift+blocked → 경고 후 False(helm 계속)."""
    ok = subprocess.CompletedProcess([], 0, "plan ok", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: ok)
    monkeypatch.setattr(
        eks_apply, "_inspect_managed_plan",
        lambda *a, **k: ([], [],
                         ["alb_target_5xx[app/x/1] (delete) — 차단"]))
    called = []
    monkeypatch.setattr(eks_apply, "_run", lambda *a, **k: called.append(a))
    # drift(required=False): apply 를 건너뛰고 False — 알람 보존 + helm 계속
    assert eks_apply._apply_managed(
        _cfg(), tmp_path, ["-target=module.alerting"], [], "drift",
        required=False, allow_alb_destroy=False) is False
    assert called == []            # terraform apply 가 실행되지 않았다
    # required=True: 배포 중단
    with pytest.raises(SystemExit):
        eks_apply._apply_managed(
            _cfg(), tmp_path, ["-target=module.alerting"], [], "missing",
            required=True, allow_alb_destroy=False)


def test_alb_dims_unmatched_hostname_is_whole_failure(monkeypatch):
    """F9b — 일부 ingress hostname 의 ALB 가 describe 에 아직 없으면
    부분 dims 대신 전체 실패 (eventual-consistency flap → 알람 삭제 방지)."""
    eks_apply._ALB_DIMS_CACHE.clear()
    items = [
        {"metadata": {"name": "a-gateway",
                      "labels": {"app.kubernetes.io/instance": "llm-gateway"}},
         "status": {"loadBalancer":
                    {"ingress": [{"hostname": "a.example.com"}]}}},
        {"metadata": {"name": "b-gateway",
                      "labels": {"app.kubernetes.io/instance": "llm-gateway"}},
         "status": {"loadBalancer":
                    {"ingress": [{"hostname": "b.example.com"}]}}},
    ]
    lbs = [{"DNSName": "a.example.com", "Type": "application",
            "LoadBalancerArn": _ARN_LB}]  # b 는 아직 조회 안 됨
    _discover_fake(monkeypatch, items=items, lbs=lbs,
                   tgs=[{"TargetGroupArn": _ARN_TG}])
    lb_dims, tg_pairs, note = eks_apply._discover_alb_dims(_alert_cfg())
    assert (lb_dims, tg_pairs) == ([], [])
    assert "미조회" in note
    eks_apply._ALB_DIMS_CACHE.clear()


def test_capture_eks_scalar_alert_emails(tmp_path, monkeypatch):
    """F6 — alert_emails = "a@b.com" 스칼라도 유효 HCL — 문자 쪼개기 금지."""
    from deploy import capture
    (tmp_path / "terraform.tfvars").write_text(
        'alert_emails = "ops@example.com"\n')
    tv = capture._tfvars(tmp_path)
    ae = tv["alert_emails"]
    emails = [str(e) for e in (ae if isinstance(ae, list) else [ae])]
    assert emails == ["ops@example.com"]


def test_ensure_cluster_secret_store_creates_and_skips(monkeypatch, tmp_path):
    """terraform 은 ESO 만 설치 — ClusterSecretStore 는 deploy 가 보장한다."""
    from deploy import eks_apply
    import subprocess as _sp
    def _rc(n):
        return _sp.CompletedProcess([], n, "", "")
    calls = []
    got_ns = {"yes": True}
    got_store = {"yes": False}
    applied = []

    def fake_quiet(argv, **kw):
        calls.append(argv)
        if "ns" in argv and "external-secrets" in argv:
            return _rc(0) if got_ns["yes"] else _rc(1)
        if "clustersecretstore" in argv:
            return _rc(0) if got_store["yes"] else _rc(1)
        return _rc(0)
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda a, **k: fake_quiet(a, **k))
    monkeypatch.setattr(
        _sp, "run",
        lambda argv, **k: applied.append(k.get("input", "")) or _rc(0))

    vals = tmp_path / "v.yaml"
    vals.write_text("externalSecrets:\n  secretStoreRef:\n    name: my-store\n")
    meta = {"values_layers": [str(vals)]}
    eks_apply.ensure_cluster_secret_store(meta, "us-west-2", "", tmp_path)
    assert applied and "name: my-store" in applied[-1]
    assert "region: us-west-2" in applied[-1]

    # 이미 있으면 apply 안 함
    applied.clear(); got_store["yes"] = True
    eks_apply.ensure_cluster_secret_store(meta, "r", "", tmp_path)
    assert applied == []

    # ESO 네임스페이스 없음 → 조용히 생략
    got_ns["yes"] = False; got_store["yes"] = False; applied.clear()
    eks_apply.ensure_cluster_secret_store(meta, "r", "", tmp_path)
    assert applied == []

    # context 전파
    got_ns["yes"] = True; calls.clear()
    eks_apply.ensure_cluster_secret_store(meta, "r", "prod-ctx", tmp_path)
    # kubectl 은 --context (--kube-context 는 helm 플래그라 kubectl 이 거부해
    # store 보장이 조용히 생략됐었다)
    assert all("--context" in c for c in calls)
    assert all("--kube-context" not in c for c in calls)


def test_ensure_secret_store_namespaced_kind(monkeypatch, tmp_path, capsys):
    """kind: SecretStore 선언 시 namespaced store 는 같은 ns 의 IRSA SA 가
    필요해 자동 생성할 수 없다 — external-secrets SA 를 가리키는 manifest 를
    만들면 namespace 가 무시돼 인증이 영영 실패한다. 존재 확인 후 경고+스킵."""
    from deploy import eks_apply
    import subprocess as _sp
    calls, applied = [], []

    def fake_quiet(argv, **kw):
        calls.append(argv)
        if "secretstore" in argv:
            return _sp.CompletedProcess([], 1, "", "")
        return _sp.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake_quiet)
    monkeypatch.setattr(
        _sp, "run",
        lambda argv, **k: applied.append(k.get("input", "")) or
        _sp.CompletedProcess([], 0, "", ""))

    vals = tmp_path / "v.yaml"
    vals.write_text(
        "externalSecrets:\n  secretStoreRef:\n"
        "    name: ns-store\n    kind: SecretStore\n")
    meta = {"values_layers": [str(vals)], "namespace": "app-ns"}
    eks_apply.ensure_cluster_secret_store(meta, "r", "", tmp_path)
    # 생성은 하지 않고 경고만 — 존재 확인은 namespaced 조회였는지
    assert applied == []
    assert "SecretStore" in capsys.readouterr().err
    assert any("secretstore" in c and "-n" in c and "app-ns" in c
               for c in calls)


def test_live_deploy_env_fullname_override(monkeypatch):
    """fullnameOverride 시 deployment 가 {override}-admin-api — release 이름
    고정 조회는 미스라 instance+component 라벨로 찾아야 한다 (14차)."""
    items = [{"metadata": {"name": "custom-admin-api",
                           "labels": {"app.kubernetes.io/instance": "myrel",
                                      "app.kubernetes.io/component": "admin-api"}},
              "spec": {"template": {"spec": {"containers": [
                  {"env": [{"name": "AGENTCORE_RUNTIME_ARN",
                            "value": "arn:aws:runtime/x"}]}]}}}},
             {"metadata": {"name": "other-admin-api",
                           "labels": {"app.kubernetes.io/instance": "other",
                                      "app.kubernetes.io/component": "admin-api"}},
              "spec": {"template": {"spec": {"containers": [
                  {"env": [{"name": "AGENTCORE_RUNTIME_ARN",
                            "value": "arn:aws:runtime/foreign"}]}]}}}}]
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp(items))
    meta = {"namespace": "ns", "release": "myrel"}
    assert (eks_apply._live_deploy_env(meta, "admin-api",
                                       "AGENTCORE_RUNTIME_ARN")
            == "arn:aws:runtime/x")


def test_strip_es_secrets_malformed_layers(monkeypatch, tmp_path):
    """values 레이어가 스칼라/리스트/스칼라 external 섹션이어도 죽지 않는다."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("- a\n- b\n")                      # 리스트 문서
    bad2 = tmp_path / "bad2.yaml"
    bad2.write_text("database: scalar\nredis:\n  external: true\n")
    meta = {"namespace": "ns", "release": "r",
            "values_layers": [str(bad), str(bad2)]}
    seen = []
    monkeypatch.setattr(
        eks_apply, "_run_quiet",
        lambda a, **k: seen.append(a) or
        subprocess.CompletedProcess([], 1, "", ""))
    eks_apply.strip_es_owned_secrets(meta, repo_root=tmp_path)  # 예외 없이 완료
    assert any("r-app" in c for c in seen)


def test_managed_overlay_emits_application_namespace(monkeypatch, tmp_path):
    """deploy.namespace 변경 시 Fargate/IRSA 가 따라가야 한다 — overlay 에
    application_namespace 를 항상 방출."""
    from deploy.render import eks as R
    # render 의 overlay 생성 경로를 직접 찾기보다, 결과 파일을 확인한다
    import yaml as _y
    monkeypatch.setattr(R, "REPO_ROOT", tmp_path)
    # 최소 차트/env 구조
    (tmp_path / "deployment/charts/llm-gateway").mkdir(parents=True)
    (tmp_path / "deployment/charts/llm-gateway/values.yaml").write_text("x: 1\n")
    vals = tmp_path / "deployment/charts/llm-gateway/values-eks-fargate-stg.yaml"
    vals.write_text("adminApi: {}\n")
    env = tmp_path / "deployment/terraform/environments/llm-gateway-stg"
    env.mkdir(parents=True)
    (env / "terraform.tfvars").write_text('environment = "stg"\n')
    from deploy import schema
    cfg = schema.from_dict({
        "version": 1, "env": "stg",
        "aws": {"region": "ap-northeast-2", "account_id": "123456789012"},
        "deploy": {"target": "eks", "size_tier": "t3",
                   "namespace": "custom-ns"},
        "features": {"notifications": {"provider": "mock"}},
        "images": {"tag": "v1"}})
    out = tmp_path / "gen/stg/eks"
    R.render(cfg, out)
    tfvars = (out / "terraform.tfvars").read_text() if (out / "terraform.tfvars").exists() \
        else (out / "managed.tfvars").read_text()
    assert 'application_namespace = "custom-ns"' in tfvars


def test_live_deploy_env_null_and_nondict_entries(monkeypatch):
    """15차 — live 컨테이너에 env: null 이거나 비-dict 항목이 있어도 ''
    반환(계약) — TypeError/AttributeError 로 apply 가 죽으면 안 된다."""
    dep = {"metadata": {"name": "r-admin-api",
                        "labels": {"app.kubernetes.io/instance": "r",
                                   "app.kubernetes.io/component": "admin-api"}},
           "spec": {"template": {"spec": {
               "containers": [{"env": None}]}}}}
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: _cp([dep]))
    assert eks_apply._live_deploy_env(
        {"namespace": "ns", "release": "r"}, "admin-api",
        "AGENTCORE_RUNTIME_ARN") == ""
    dep["spec"]["template"]["spec"]["containers"][0]["env"] = [
        "not-a-dict", {"name": "AGENTCORE_RUNTIME_ARN", "value": "arn:x"}]
    assert eks_apply._live_deploy_env(
        {"namespace": "ns", "release": "r"}, "admin-api",
        "AGENTCORE_RUNTIME_ARN") == "arn:x"


def test_human_declared_chat_agent_all_channels(tmp_path, monkeypatch):
    """15차 — 사람 소유 판정은 HCL("true" 포함)·JSON tfvars·TF_VAR 전
    채널을 본다 — 놓치면 Runtime 삭제/재생 루프."""
    h = eks_apply._human_declared_chat_agent
    assert h(tmp_path) is False
    (tmp_path / "terraform.tfvars").write_text('enable_chat_agent = "true"\n')
    assert h(tmp_path) is True
    (tmp_path / "terraform.tfvars").write_text('# enable_chat_agent = true\n')
    assert h(tmp_path) is False  # 주석은 선언이 아니다
    (tmp_path / "terraform.tfvars").unlink()
    (tmp_path / "x.auto.tfvars.json").write_text('{"enable_chat_agent": "true"}')
    assert h(tmp_path) is True
    (tmp_path / "x.auto.tfvars.json").write_text('{"enable_chat_agent": true}')
    assert h(tmp_path) is True
    (tmp_path / "x.auto.tfvars.json").unlink()
    monkeypatch.setenv("TF_VAR_enable_chat_agent", "true")
    assert h(tmp_path) is True


def test_warn_missing_ecr_tags(monkeypatch, capsys):
    """15차 — apply 경로는 이미지를 push 하지 않는다. 선언 태그가 ECR 에
    없으면 ImagePullBackOff 가 확정이라 preview 에서 경고해야 한다."""
    from deploy import schema
    cfg = schema.from_dict({
        "version": 1, "env": "dev",
        "aws": {"region": "ap-northeast-2"},
        "deploy": {"target": "eks", "size_tier": "t3"},
        "images": {"tag": "v9"}})

    def fake(argv, **k):
        if "llm-gateway/admin-api" in argv:
            return subprocess.CompletedProcess(
                [], 1, "", "ImageNotFoundException")
        return subprocess.CompletedProcess([], 0, "{}", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    eks_apply._warn_missing_ecr_tags(cfg, "ap-northeast-2")
    assert "admin-api:v9" in capsys.readouterr().err
    # 비-ECR 레지스트리는 describe-images 가 안 통하니 스킵
    cfg2 = schema.from_dict({
        "version": 1, "env": "dev", "aws": {"region": "ap-northeast-2"},
        "deploy": {"target": "eks", "size_tier": "t3"},
        "images": {"tag": "v9", "registry": "harbor.example.com/x"}})
    called = []
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda *a, **k: called.append(a) or
                        subprocess.CompletedProcess([], 0, "{}", ""))
    eks_apply._warn_missing_ecr_tags(cfg2, "ap-northeast-2")
    assert not called


def test_find_domain_cert_prefers_issued(monkeypatch):
    """16차 — 같은 도메인의 cert 가 여러 장이면 ISSUED 를 우선한다.
    첫 매치가 PENDING/FAILED 이면 사용 가능한 인증서를 영영 못 집는다."""
    certs = {"arn:1": {"S": "FAILED", "SAN": ["*.gw.example.com"]},
             "arn:2": {"S": "ISSUED", "SAN": ["*.gw.example.com", "gw.example.com"]}}

    def fake(argv, **k):
        if "list-certificates" in argv:
            return subprocess.CompletedProcess(
                [], 0, "arn:1 arn:2", "")
        arn = argv[argv.index("--certificate-arn") + 1]
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"S": certs[arn]["S"],
                               "SAN": certs[arn]["SAN"]}), "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    assert eks_apply._find_domain_cert("gw.example.com", "r") == ("arn:2", "ISSUED")


def test_declared_nextauth_url_detects_env_layer(tmp_path):
    """16차 — values 레이어가 nextauthUrl 을 선언하면 2-phase ALB 주입을
    스킵해야 한다 — --set 이 선언 URL 을 매번 덮어쓰기 때문."""
    layer = tmp_path / "values-eks-dev.yaml"
    layer.write_text("adminUi:\n  nextauthUrl: https://admin.internal.example.com\n")
    meta = {"values_layers": ["values-eks-dev.yaml"]}
    assert eks_apply._declared_nextauth_url(meta, tmp_path) == \
        "https://admin.internal.example.com"
    layer.write_text("adminUi:\n  other: x\n")
    assert eks_apply._declared_nextauth_url(meta, tmp_path) == ""
    layer.write_bytes(b"\xff\xfe not utf8")
    assert eks_apply._declared_nextauth_url(meta, tmp_path) == ""  # 크래시 없이 빈값


def test_strip_es_chart_default_db_secret(monkeypatch, tmp_path):
    """16차 — release 가 llm-gateway 가 아니고 passwordSecretName 미선언이면
    차트 기본 llm-gateway-db 가 strip 후보에 있어야 한다."""
    chart = tmp_path / "deployment" / "charts" / "llm-gateway"
    chart.mkdir(parents=True)
    (chart / "values.yaml").write_text(
        "database:\n  external:\n    passwordSecretName: llm-gateway-db\n"
        "redis:\n  external:\n    passwordSecretName: \"\"\n")
    meta = {"namespace": "ns", "release": "acme", "values_layers": []}
    seen = []

    def fake(argv, **k):
        seen.append(argv)
        return subprocess.CompletedProcess([], 0, "ExternalSecret", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    eks_apply.strip_es_owned_secrets(meta, repo_root=tmp_path)
    queried = {a[a.index("secret") + 1] for a in seen if "secret" in a
               and "get" in a}
    assert "llm-gateway-db" in queried
    assert "acme-app" in queried


def test_migration_image_malformed_layer(monkeypatch, tmp_path):
    """16차 — 스칼라/리스트 레이어·깨진 yaml 이 있어도 migration 이미지
    도출이 AttributeError/YAMLError 로 죽지 않아야 한다."""
    from deploy import schema
    bad = tmp_path / "bad.yaml"
    bad.write_text("migration: just-a-string\nglobal: [not, a, map]\n")
    bad2 = tmp_path / "bad2.yaml"
    bad2.write_text("{{{ not yaml")
    meta = {"values_layers": [str(bad), str(bad2)]}
    cfg = schema.from_dict({"version": 1, "env": "dev",
                            "aws": {"region": "ap-northeast-2"},
                            "deploy": {"target": "eks", "size_tier": "t3"},
                            "images": {"tag": "v1"}})
    monkeypatch.setattr(eks_apply, "_aws_account_id", lambda r: "")
    assert eks_apply._migration_image(cfg, meta, tmp_path, tmp_path) == ""


def test_selfsigned_cert_only_reuses_issued(monkeypatch):
    """16차 — EXPIRED/PENDING 자체서명 인증서는 재사용하면 안 된다 —
    ALB 리스너 부착이 거절돼 ingress sync 가 실패한다. 조회 쿼리에
    Status=='ISSUED' 필터가 있어야 한다."""
    queries = []

    def fake(argv, **k):
        queries.append(argv)
        if "list-certificates" in argv:
            return subprocess.CompletedProcess([], 0, "arn:aws:acm:cert/ok", "")
        return subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(eks_apply, "_run_quiet", fake)
    arn = eks_apply.ensure_selfsigned_cert("dev", "ap-northeast-2")
    assert arn == "arn:aws:acm:cert/ok"
    q = next(a for a in queries if "list-certificates" in a)
    assert any("Status=='ISSUED'" in a for a in q)
    # 미발급이면 import 경로로 — openssl 실패 시 빈 문자열(부분적 실패 허용)
    def no_cert(argv, **k):
        if "list-certificates" in argv:
            return subprocess.CompletedProcess([], 0, "None", "")
        return subprocess.CompletedProcess([], 1, "", "boom")
    monkeypatch.setattr(eks_apply, "_run_quiet", no_cert)
    assert eks_apply.ensure_selfsigned_cert("dev", "ap-northeast-2") == ""


def test_chatagent_env_scrubs_stale_provisioner_inputs(monkeypatch):
    """17차 — 부모 쉘의 stale export 가 outputs 를 이기면 eks 배포가
    ecs run-task 로 ALTER 하거나 엉뚱한 secret 을 쓴다. 프로비저너 입력
    키는 스크럽 후 명시된 값만 주입돼야 한다."""
    from deploy import chatagent
    for k in ("ECS_PW_TASKDEF_ARN", "KUBE_CONTEXT", "ECR_REPO",
              "DB_MASTER_SECRET", "TARGET_NAME", "AWS_REGION",
              "AGENT_NAME", "MIGRATION_IMAGE"):
        monkeypatch.setenv(k, "stale")
    monkeypatch.setenv("CHAT_AGENT_REGION", "us-west-2")
    e = chatagent._env({"chat_agent_ecr_url": "real.ecr/repo"},
                       {"namespace": "ns"}, "", context="ctx",
                       env_name="dev", region="ap-south-1")
    assert e["ECR_REPO"] == "real.ecr/repo"
    assert e["KUBE_CONTEXT"] == "ctx"          # 명시 인자가 산다
    assert e["K8S_NAMESPACE"] == "ns"
    assert "ECS_PW_TASKDEF_ARN" not in e        # eks 경로에서 스크럽
    assert "DB_MASTER_SECRET" not in e
    assert "TARGET_NAME" not in e
    assert e["REGION"] == "us-west-2"           # env 우선 계약 유지
    assert e["AGENT_NAME"] == "llm_gateway_dev_admin_chat_agent"


def test_websearch_env_scrubs_target_name(monkeypatch):
    """17차 — stale TARGET_NAME 이 있으면 다른 이름의 target 이 생기는데
    주입값은 web-search-tool 고정이라 도구를 못 찾는다."""
    from deploy import websearch
    monkeypatch.setenv("TARGET_NAME", "stale-tool")
    monkeypatch.setenv("REGION", "ap-south-1")
    e = websearch._env("dev")
    assert "TARGET_NAME" not in e
    assert e["REGION"] == "us-east-1"           # 핀 고정은 유지
    assert e["GW_NAME"] == "llm-gateway-websearch-dev"


def test_status_url_requires_ready_gateway_and_target(monkeypatch):
    """17차 — URL 만 출력되는 부분 삭제 상태(target 없음/FAILED)를
    '존재'로 판정하면 ensure 가 deploy 를 영영 안 돌려 자가치유 불가."""
    from deploy import websearch

    def fake(stdout):
        return lambda *a, **k: subprocess.CompletedProcess([], 0, stdout, "")
    base = "gateway: gw-1 status=READY authorizerType=AWS_IAM\n" \
           "  url: https://gw.example/mcp\n"
    # target NONE — 부분 삭제 → "" (deploy 수렴 유도)
    monkeypatch.setattr(websearch, "_run", fake(base + "  target: NONE\n"))
    assert websearch.status_url("dev", __import__("pathlib").Path("/x")) == ""
    # target FAILED → "" (재생성 필요)
    monkeypatch.setattr(websearch, "_run",
                        fake(base + "  target: t1 status=FAILED\n"))
    assert websearch.status_url("dev", __import__("pathlib").Path("/x")) == ""
    # target READY → URL 반환
    monkeypatch.setattr(websearch, "_run",
                        fake(base + "  target: t1 status=READY\n"))
    assert websearch.status_url("dev", __import__("pathlib").Path("/x")) == \
        "https://gw.example/mcp"
    # gateway FAILED → target 있어도 ""
    monkeypatch.setattr(websearch, "_run", fake(
        "gateway: gw-1 status=FAILED\n  url: https://gw.example/mcp\n"
        "  target: t1 status=READY\n"))
    assert websearch.status_url("dev", __import__("pathlib").Path("/x")) == ""


def test_tf_init_always_reconfigures_backend(monkeypatch, tmp_path):
    """19차 — .terraform 캐시가 있어도 선언된 backend 로 -reconfigure 한다.
    캐시 재사용은 yaml 의 bucket/table 변경이나 env 디렉터리 공유 시 낡은
    state 에 붙는 cross-env 오적을 만든다."""
    (tmp_path / ".terraform").mkdir()  # 캐시 존재 시뮬레이션
    calls = []
    monkeypatch.setattr(
        eks_apply, "_run_quiet",
        lambda argv, **k: (calls.append(argv),
                           subprocess.CompletedProcess(argv, 0, "", ""))[1])
    from deploy import schema
    cfg = schema.from_dict({
        "version": 1, "env": "dev",
        "aws": {"region": "ap-northeast-2"},
        "images": {"tag": "v1"},
        "deploy": {"target": "eks", "size_tier": "t3",
                   "tfstate_bucket": "my-bkt", "tfstate_table": "my-tbl"}})
    eks_apply._tf_init(cfg, tmp_path)
    argv = calls[0]
    assert "-reconfigure" in argv
    assert "-backend-config=bucket=my-bkt" in argv
    assert "-backend-config=dynamodb_table=my-tbl" in argv


def test_load_meta_rejects_nonmapping(tmp_path):
    """19차 — deploy.yaml 이 리스트/스칼라면 .get 에서 AttributeError 대신
    명확한 SystemExit."""
    g = tmp_path / "gen"
    g.mkdir()
    (g / "deploy.yaml").write_text("- just\n- a\n- list\n")
    with pytest.raises(SystemExit, match="매핑"):
        eks_apply.load_meta(g, tmp_path)
    (g / "deploy.yaml").write_text("just a scalar\n")
    with pytest.raises(SystemExit, match="매핑"):
        eks_apply.load_meta(g, tmp_path)


def test_load_meta_rejects_scalar_values_layers(tmp_path):
    """19차 — values_layers 가 문자열이면 문자 단위 순회 대신 명확한 오류."""
    g = tmp_path / "gen"
    g.mkdir()
    (g / "deploy.yaml").write_text(
        "release: r\nnamespace: n\nchart: c\nvalues_layers: values.yaml\n")
    with pytest.raises(SystemExit, match="values_layers"):
        eks_apply.load_meta(g, tmp_path)


def test_live_deploy_env_prefers_service_named_container(monkeypatch):
    """20차 — 사이드카(otel 등)가 containers[0] 이면 앱 env 가 아니라
    사이드카 env 를 읽어 보존값이 빈다 — 서비스명 컨테이너를 골라야 한다."""
    dep = {"metadata": {"name": "rel-admin-api",
                        "labels": {"app.kubernetes.io/instance": "rel"}},
           "spec": {"template": {"spec": {"containers": [
               {"name": "otel-collector", "env": [
                   {"name": "AGENTCORE_GATEWAY_URL", "value": "sidecar"}]},
               {"name": "admin-api", "env": [
                   {"name": "AGENTCORE_GATEWAY_URL", "value": "app-url"}]},
           ]}}}}
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda *a, **k: _cp([dep]))
    assert eks_apply._live_deploy_env(
        {"namespace": "ns", "release": "rel"},
        "admin-api", "AGENTCORE_GATEWAY_URL") == "app-url"


def test_declared_environment_all_channels(tmp_path):
    """21차 — terraform 이 읽는 전 채널에서 environment 를 읽는다:
    terraform.tfvars 만 보면 auto.tfvars/json 선언을 놓쳐 '판독 불가'로 오인."""
    (tmp_path / "terraform.tfvars").write_text('# env comment\nfoo = "bar"\n')
    assert eks_apply._declared_environment(tmp_path) == ""
    (tmp_path / "x.auto.tfvars").write_text('environment = "prod"\n')
    assert eks_apply._declared_environment(tmp_path) == "prod"
    (tmp_path / "x.auto.tfvars").write_text('other = 1\n')
    (tmp_path / "terraform.tfvars.json").write_text('{"environment": "stg"}')
    assert eks_apply._declared_environment(tmp_path) == "stg"


def test_declared_environment_ignores_comments(tmp_path):
    # 주석 안의 죽은 선언은 environment 로 오인하면 안 된다 (과잉 차단)
    (tmp_path / "terraform.tfvars").write_text(
        '# environment = "dev"\n/* environment = "x" */\nreal = 1\n')
    assert eks_apply._declared_environment(tmp_path) == ""


def test_check_env_dir_declared_hard_refuses_mismatch(tmp_path):
    (tmp_path / "x.auto.tfvars").write_text('environment = "other-env"\n')
    cfg = types.SimpleNamespace(env="dev")
    with pytest.raises(SystemExit, match="other-env"):
        eks_apply._check_env_dir_declared(cfg, tmp_path, hard=True)
    # soft 모드는 경고만 — raise 안 함
    eks_apply._check_env_dir_declared(cfg, tmp_path, hard=False)


def test_check_env_dir_declared_match_passes(tmp_path):
    (tmp_path / "terraform.tfvars").write_text('environment = "dev"\n')
    cfg = types.SimpleNamespace(env="dev")
    eks_apply._check_env_dir_declared(cfg, tmp_path, hard=True)


def test_check_context_cluster_mismatch_refuses(monkeypatch):
    """--context 가 backend 와 다른 클러스터를 가리키면 hijack 방지로 차단."""
    r = types.SimpleNamespace(returncode=0,
                              stdout="arn:aws:eks:us-east-1:1:cluster/other")
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: r)
    outs = {"cluster_name": "this-cluster"}
    eks_apply._check_context_cluster(outs, "myctx", hard=False)  # 경고만
    with pytest.raises(SystemExit, match="other"):
        eks_apply._check_context_cluster(outs, "myctx", hard=True)


def test_check_context_cluster_match_and_unresolvable_pass(monkeypatch):
    """일치하거나(이름/ARN 상관없이) 해석 불가면 통과."""
    r = types.SimpleNamespace(returncode=0,
                              stdout="arn:aws:eks:us-east-1:1:cluster/this-cluster")
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: r)
    outs = {"cluster_name": "this-cluster"}
    eks_apply._check_context_cluster(outs, "myctx", hard=True)
    # context 미지정/미해석/backend cluster_name 없음 — 전부 조기 통과
    eks_apply._check_context_cluster(outs, "", hard=True)
    eks_apply._check_context_cluster({}, "myctx", hard=True)
    bad = types.SimpleNamespace(returncode=1, stdout="")
    monkeypatch.setattr(eks_apply, "_run_quiet", lambda *a, **k: bad)
    eks_apply._check_context_cluster(outs, "myctx", hard=True)


def test_declared_environment_last_wins(tmp_path):
    """terraform 은 last-file-wins — override 선언이 실효값이다."""
    (tmp_path / "terraform.tfvars").write_text('environment = "dev"\n')
    # 자동로드 순서상 zzz.auto.tfvars 가 마지막 — 실효는 prod
    (tmp_path / "zzz.auto.tfvars").write_text('environment = "prod"\n')
    assert eks_apply._declared_environment(tmp_path) == "prod"
    # terraform.tfvars.json 도 override 가능 — tfvars 뒤, auto 앞
    (tmp_path / "terraform.tfvars.json").write_text('{"environment": "json-env"}')
    (tmp_path / "zzz.auto.tfvars").unlink()
    assert eks_apply._declared_environment(tmp_path) == "json-env"


def test_declared_environment_skips_varfile_only_json(tmp_path):
    """prod.tfvars.json 같은 -var-file 전용 파일은 자동로드가 아니다."""
    (tmp_path / "prod.tfvars.json").write_text('{"environment": "prod"}')
    (tmp_path / "terraform.tfvars").write_text('environment = "dev"\n')
    assert eks_apply._declared_environment(tmp_path) == "dev"
    # auto.tfvars.json 은 자동로드 — 스캔 대상
    (tmp_path / "x.auto.tfvars.json").write_text('{"environment": "auto"}')
    assert eks_apply._declared_environment(tmp_path) == "auto"


def test_teardown_checks_context_before_helm(monkeypatch):
    """23차 — teardown 은 helm status/uninstall 전에 kube context 가 backend
    클러스터인지 대조한다. mismatch 면 같은 이름의 release 를 가진 남의
    클러스터를 내리지 않게 helm 호출 자체가 없어야 한다."""
    from deploy import cli
    called = []
    monkeypatch.setattr(eks_apply, "load_meta",
                        lambda *a, **k: {"release": "gw", "namespace": "gw",
                                         "env_dir": "tf/envs/eks/dev",
                                         "env": "dev"})
    monkeypatch.setattr(eks_apply, "_declared_environment", lambda d: "")
    monkeypatch.setattr(eks_apply, "_tf_init",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"cluster_name": "real-cluster"})
    monkeypatch.setattr(eks_apply, "_kube_context_cluster", lambda c: "other-cluster")
    monkeypatch.setattr(eks_apply, "_run_quiet",
                        lambda argv, **k: (called.append(argv),
                                           types.SimpleNamespace(returncode=0))[1])
    from deploy import schema
    cfg = schema.from_dict({"version": 1, "env": "dev",
                            "aws": {"region": "us-east-1"},
                            "deploy": {"target": "eks", "size_tier": "t3"},
                            "images": {"tag": "v1"}})
    args = types.SimpleNamespace(context="victim-ctx", infra=False, yes=True)
    rc = cli._teardown_eks(cfg, args)
    assert rc == 1
    assert not any(argv and argv[0] == "helm" for argv in called)


def test_bootstrap_checks_context_before_alias(monkeypatch, tmp_path):
    """23차 — bootstrap 은 update-kubeconfig --alias 가 기존 context 를
    덮어쓰기 전에 _check_context_cluster 로 대조한다. 불일치면 update_kubeconfig
    에 도달하면 안 된다."""
    from deploy import cli, bootstrap
    from deploy.render import eks as eks_render
    from deploy import schema
    cfg_path = tmp_path / "gateway.yaml"
    cfg_path.write_text("env: dev\n")
    cfg = schema.from_dict({"version": 1, "env": "dev",
                            "aws": {"region": "us-east-1"},
                            "deploy": {"target": "eks", "size_tier": "t3"},
                            "images": {"tag": "v1"}})
    monkeypatch.setattr(cli, "_load_config", lambda a: cfg)
    monkeypatch.setattr(schema, "load", lambda p: cfg)
    monkeypatch.setattr(bootstrap, "scaffold_eks_env", lambda *a, **k: "envdir")
    monkeypatch.setattr(eks_render, "render", lambda *a, **k: {"notes": []})
    monkeypatch.setattr(cli, "_confirm_apply", lambda a: True)
    monkeypatch.setattr(bootstrap, "ensure_tfstate", lambda *a, **k: ("b", "t"))
    monkeypatch.setattr(bootstrap, "env_apply",
                        lambda *a, **k: {"cluster_name": "real-cluster"})

    def boom(outs, context, hard=False):
        raise SystemExit("context points at other cluster")
    monkeypatch.setattr(eks_apply, "_check_context_cluster", boom)
    aliased = []
    monkeypatch.setattr(bootstrap, "update_kubeconfig",
                        lambda *a, **k: aliased.append(a))
    args = types.SimpleNamespace(config=str(cfg_path), context="victim-ctx",
                                 yes=True, plan=False)
    with pytest.raises(SystemExit, match="other cluster"):
        cli.cmd_bootstrap(args)
    assert aliased == []


def test_declared_environment_unquoted_and_name_key(tmp_path):
    """23차 — terraform 은 비인용 값(숫자 등)도 선언으로 수용한다. 따옴표만
    보면 선언을 놓쳐 외부 state 로 과잉 통과한다. environment_name 같은
    접두 키는 environment 가 아니다."""
    (tmp_path / "terraform.tfvars").write_text('environment = 5\n')
    assert eks_apply._declared_environment(tmp_path) == "5"
    (tmp_path / "terraform.tfvars").write_text('environment_name = "x"\n')
    assert eks_apply._declared_environment(tmp_path) == ""


def test_plan_blocks_context_mismatch_before_alias(monkeypatch, tmp_path):
    """23차 — plan() 은 update-kubeconfig --alias 전에 context 대조를 해야
    한다. soft 경고만 하고 alias 를 쓰면 preview(--plan) 한 번으로도
    kubeconfig 가 변형되고 apply 의 hard 검사가 재지정된 context 를 읽어
    무력화된다."""
    from deploy import bootstrap
    meta = {"release": "gw", "namespace": "gw",
            "env_dir": str(tmp_path), "env": "dev"}
    monkeypatch.setattr(eks_apply, "load_meta", lambda *a, **k: dict(meta))
    monkeypatch.setattr(eks_apply, "_check_env_dir_declared", lambda *a, **k: None)
    monkeypatch.setattr(eks_apply, "_tf_init",
                        lambda *a, **k: types.SimpleNamespace(returncode=0))
    monkeypatch.setattr(eks_apply, "tf_outputs",
                        lambda d: {"cluster_name": "real-cluster"})
    monkeypatch.setattr(eks_apply, "_kube_context_cluster", lambda c: "other-cluster")
    aliased = []
    monkeypatch.setattr(bootstrap, "update_kubeconfig",
                        lambda *a, **k: aliased.append(a))
    from deploy import schema
    cfg = schema.from_dict({"version": 1, "env": "dev",
                            "aws": {"region": "us-east-1"},
                            "deploy": {"target": "eks", "size_tier": "t3"},
                            "images": {"tag": "v1"}})
    with pytest.raises(SystemExit, match="other-cluster"):
        eks_apply.plan(cfg, tmp_path, tmp_path, "victim-ctx")
    assert aliased == []
