# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks_apply — helm/kubectl 호출 경계 단위 테스트."""
import json
import subprocess

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
                        lambda cfg, gen, env, outs: outs)
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
