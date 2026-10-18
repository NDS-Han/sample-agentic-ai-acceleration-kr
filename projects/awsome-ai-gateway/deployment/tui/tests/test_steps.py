from deployment.tui import steps
from deployment.tui.config import BackendConfig

BE = BackendConfig(bucket="b", dynamodb_table="t")


def _names(wf):
    return [s.name for s in wf]


def test_llm_workflow_includes_build_lambdas_when_chat_db_enabled():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=True, flags={})
    assert "build-lambdas" in _names(wf)


def test_llm_workflow_omits_build_lambdas_when_chat_db_disabled():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False, flags={})
    assert "build-lambdas" not in _names(wf)


def test_tf_init_injects_backend_config():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False, flags={})
    tf_init = next(s for s in wf if s.name == "tf-init")
    assert "-backend-config=bucket=b" in tf_init.argv
    assert "-backend-config=dynamodb_table=t" in tf_init.argv


def test_install_eks_isolates_kubeconfig():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False,
                                  flags={}, cluster_name="my-cluster")
    install = next(s for s in wf if s.name == "install-eks")
    assert install.env["KUBECONFIG"] == "/tmp/my-cluster.kubeconfig"


def test_verify_migration_shares_install_kubeconfig():
    # smoke-test 스텝도 install-eks와 동일 격리 KUBECONFIG를 써야 한다. 없으면
    # 기본 ~/.kube/config(다른 클러스터로 오염됨)를 봐서 pod를 못 찾고 오탐한다.
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False,
                                  flags={}, cluster_name="my-cluster")
    verify = next(s for s in wf if s.name == "verify-migration")
    assert verify.env["KUBECONFIG"] == "/tmp/my-cluster.kubeconfig"


def test_install_eks_passes_flags_as_env():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=False,
                                  flags={"DEBUG_MODE": True, "MIGRATION_ENABLED": False})
    install = next(s for s in wf if s.name == "install-eks")
    assert install.env["DEBUG_MODE"] == "true"
    assert install.env["MIGRATION_ENABLED"] == "false"


def test_llm_workflow_order():
    wf = steps.build_llm_workflow(env="dev", backend=BE, enable_chat_db_tools=True, flags={})
    names = _names(wf)
    assert names.index("build-lambdas") < names.index("tf-init")
    assert names.index("tf-apply") < names.index("install-eks")
    assert names.index("install-eks") < names.index("verify-migration")


def test_tool_workflow_order():
    wf = steps.build_tool_workflow(backend=BE)
    names = _names(wf)
    assert names.index("tf-init") < names.index("tf-apply")


def test_tool_workflow_injects_key_file_env_not_tfvars():
    # ★함정: API 키는 tfvars가 아니라 TOOL_KEY_FILE env로만 넘어가야 한다
    #   (tfstate 평문 저장 + undeclared variable 경고 방지).
    from pathlib import Path

    wf = steps.build_tool_workflow(backend=BE, key_file=Path("/tmp/keys.env"))
    apply = next(s for s in wf if s.name == "tf-apply")
    assert apply.env["TOOL_KEY_FILE"] == "/tmp/keys.env"


def test_tool_workflow_no_key_file_leaves_env_unset():
    # 키가 없으면(예: DuckDuckGo만) TOOL_KEY_FILE을 주입하지 않는다 → seed 스킵.
    wf = steps.build_tool_workflow(backend=BE)
    apply = next(s for s in wf if s.name == "tf-apply")
    assert apply.env is None


def test_llm_teardown_order_helm_before_destroy():
    # ★함정: helm uninstall(ALB 제거)이 terraform destroy보다 먼저여야 VPC가 지워진다
    wf = steps.build_llm_teardown(env="dev", backend=BE)
    names = _names(wf)
    assert names.index("helm-uninstall") < names.index("tf-init")
    assert names.index("tf-init") < names.index("tf-destroy")


def test_llm_teardown_isolates_kubeconfig_and_not_skippable():
    wf = steps.build_llm_teardown(env="dev", backend=BE, cluster_name="my-cluster")
    helm = next(s for s in wf if s.name == "helm-uninstall")
    assert helm.env["KUBECONFIG"] == "/tmp/my-cluster.kubeconfig"
    # "클러스터/릴리스 없음"은 스크립트가 exit 0으로 스킵 — 실패(ALB/Ingress 잔존)는
    # destroy를 막아야 한다 (R24-S8: ALB 고아 → VPC DependencyViolation).
    assert helm.skippable is False


def test_llm_teardown_waits_for_alb_delete():
    """helm uninstall 이 반환돼도 ALB controller finalizer는 비동기 — destroy 전에
    Ingress 삭제 완료까지 기다려야 orphan ENI/SG 없이 VPC가 지워진다 (R24-S8)."""
    script = steps._HELM_UNINSTALL
    assert "--wait" in script
    assert "kubectl wait --for=delete ingress" in script


def test_llm_teardown_destroy_injects_backend():
    wf = steps.build_llm_teardown(env="dev", backend=BE)
    tf_init = next(s for s in wf if s.name == "tf-init")
    assert "-backend-config=bucket=b" in tf_init.argv


def test_llm_teardown_helm_step_gets_backend_args_and_fails_closed():
    """helm-uninstall 은 tf-init 보다 먼저 도므로 backend init 용 인자가 env 로
    전달돼야 한다(R24-N8r4). init 실패 시 스킵은 fail-open 이라 스크립트는
    init 실패를 exit 1 로 보낸다."""
    wf = steps.build_llm_teardown(env="dev", backend=BE)
    helm = next(s for s in wf if s.name == "helm-uninstall")
    assert "-backend-config=bucket=b" in helm.env["TF_BACKEND_ARGS"]
    script = steps._HELM_UNINSTALL
    assert "terraform init -input=false -reconfigure $TF_BACKEND_ARGS" in script
    # init 실패를 스킵(exit 0)으로 삼키지 않는다 — state 존재 불명이면 중단.
    # 주석에 "terraform output"이 먼저 나오므로 실제 명령 문자열로 위치 비교.
    assert script.index("terraform init -input=false") < script.index("terraform output -json")


def test_tool_teardown_calls_provision_script():
    wf = steps.build_tool_teardown()
    assert wf[0].name == "tool-teardown"
    assert "teardown" in wf[0].argv


def test_tool_teardown_cleans_up_credential_providers_after_destroy():
    # ★재발 방지: destroy 후 orphan credential provider 정리 스텝이 뒤따라야 함
    wf = steps.build_tool_teardown()
    names = _names(wf)
    assert names == ["tool-teardown", "cleanup-credential-providers"]
    cleanup = wf[1]
    assert cleanup.skippable is True  # best-effort — 없으면 조용히 통과


def test_cleanup_targets_only_managed_provider_names():
    # 다른 스택의 `-creds` 접미 provider나 무관 provider를 오삭제하면 안 됨
    wf = steps.build_tool_teardown()
    script = wf[1].argv[-1]
    for name in steps.MANAGED_CREDENTIAL_PROVIDERS:
        assert name in script
    # 문자열 완전 일치 비교로 부분일치 오삭제 방지 (grep -w는 '-'를 경계로 오탐)
    assert '[ "$e" = "$n" ]' in script
    assert "grep -w" not in script
    # 이 스택이 만들지 않는 이름은 정리 대상에 없어야 함
    assert "brave-creds" not in steps.MANAGED_CREDENTIAL_PROVIDERS
    assert "NasaInsightAPIKey" not in steps.MANAGED_CREDENTIAL_PROVIDERS


def test_cleanup_exact_match_does_not_target_creds_suffix():
    # tavily가 tavily-creds에 오탐되지 않도록 완전 일치 로직 검증 (실 셸 실행)
    import subprocess

    script = steps.build_tool_teardown()[1].argv[-1]
    # aws를 가짜로 대체: list는 -creds 이름만 반환, delete 호출은 기록
    harness = (
        'aws() {\n'
        '  if [ "$2" = "list-api-key-credential-providers" ]; then\n'
        '    echo "NasaInsightAPIKey brave-creds tavily-creds"; return 0; fi\n'
        '  if [ "$2" = "delete-api-key-credential-provider" ]; then\n'
        '    echo "DELETE_CALLED $4"; return 0; fi\n'
        '}\n'
    )
    out = subprocess.run(
        ["bash", "-c", harness + script],
        capture_output=True, text=True,
    ).stdout
    assert "DELETE_CALLED" not in out  # -creds 이름은 절대 삭제 대상 아님
