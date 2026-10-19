# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""pricinglambda — LiteLLM 단가 Lambda 프로비저닝 단위 테스트."""
import types

import pytest

from deploy import pricinglambda


class _NoSuch(Exception):
    pass


class _NotFound(Exception):
    pass


class FakeIam:
    def __init__(self):
        self.exceptions = types.SimpleNamespace(NoSuchEntityException=_NoSuch)
        self.roles = {}
        self.attached = []
        self.inline = {}

    def get_role(self, RoleName):
        if RoleName not in self.roles:
            raise _NoSuch(RoleName)
        return self.roles[RoleName]

    def create_role(self, RoleName, **kw):
        self.roles[RoleName] = kw

    def attach_role_policy(self, RoleName, PolicyArn):
        self.attached.append((RoleName, PolicyArn))

    def put_role_policy(self, RoleName, PolicyName, PolicyDocument):
        self.inline[(RoleName, PolicyName)] = PolicyDocument

    def get_role_policy(self, RoleName, PolicyName):
        if (RoleName, PolicyName) not in self.inline:
            raise _NoSuch(PolicyName)

    def delete_role_policy(self, RoleName, PolicyName):
        self.inline.pop((RoleName, PolicyName), None)

    def detach_role_policy(self, RoleName, PolicyArn):
        try:
            self.attached.remove((RoleName, PolicyArn))
        except ValueError:
            raise _NoSuch(PolicyArn)

    def list_role_policies(self, RoleName):
        return {"PolicyNames": [p for (r, p) in self.inline if r == RoleName]}

    def delete_role(self, RoleName):
        self.roles.pop(RoleName, None)

    def get_waiter(self, _):
        return types.SimpleNamespace(wait=lambda **k: None)


class FakeLambda:
    def __init__(self):
        self.exceptions = types.SimpleNamespace(
            ResourceNotFoundException=_NotFound)
        self.functions = {}
        self.calls = []

    def get_function(self, FunctionName):
        if FunctionName not in self.functions:
            raise _NotFound(FunctionName)
        return self.functions[FunctionName]

    def create_function(self, **kw):
        self.functions[kw["FunctionName"]] = kw
        self.calls.append("create")

    def update_function_code(self, **kw):
        self.calls.append("update_code")

    def update_function_configuration(self, **kw):
        self.calls.append("update_config")

    def delete_function(self, FunctionName):
        self.functions.pop(FunctionName, None)

    def get_waiter(self, _):
        return types.SimpleNamespace(wait=lambda **k: None)


class FakeSts:
    def get_caller_identity(self):
        return {"Account": "123456789012"}


@pytest.fixture
def fakes(monkeypatch):
    lam, iam = FakeLambda(), FakeIam()
    monkeypatch.setattr(pricinglambda, "_clients",
                        lambda region: (lam, iam, FakeSts()))
    monkeypatch.setattr("time.sleep", lambda s: None)
    return lam, iam


def _cfg(**over):
    from deploy import schema
    doc = {
        "version": 1, "env": "dev",
        "aws": {"region": "ap-northeast-2", "account_id": "123456789012"},
        "deploy": {"target": "eks", "size_tier": "t3"},
        "features": {"notifications": {"provider": "mock"},
                     "pricing_lambda": True},
        "images": {"tag": "v1"},
    }
    doc.update(over)
    return schema.from_dict(doc)


def test_ensure_creates_role_fn_and_invoke_policy(fakes, tmp_path):
    lam, iam = fakes
    (tmp_path / "litellm-pricing-lambda").mkdir()
    (tmp_path / "litellm-pricing-lambda/lambda_function.py").write_text("x=1")
    fn = pricinglambda.ensure(
        _cfg(), tmp_path,
        admin_role_arn="arn:aws:iam::123456789012:role/llm-gateway-dev-admin-api")
    assert fn == "llm-gateway-dev-litellm-pricing"
    assert "create" in lam.calls
    # role 은 BasicExecutionRole 만 — DB/Secret 접근 없음 (ops 와 동일)
    assert iam.attached == [("llm-gateway-dev-litellm-pricing",
                           pricinglambda.BASIC_EXEC)]
    pol = iam.inline[("llm-gateway-dev-admin-api",
                      pricinglambda.INVOKE_POLICY)]
    assert "InvokeFunction" in pol
    assert "function:llm-gateway-dev-litellm-pricing" in pol


def test_ensure_existing_fn_updates_code_only(fakes, tmp_path):
    lam, iam = fakes
    lam.functions["llm-gateway-dev-litellm-pricing"] = {}
    iam.roles["llm-gateway-dev-litellm-pricing"] = {}
    (tmp_path / "litellm-pricing-lambda").mkdir()
    (tmp_path / "litellm-pricing-lambda/lambda_function.py").write_text("x=1")
    pricinglambda.ensure(_cfg(), tmp_path, attach_invoke=False)
    assert "create" not in lam.calls
    assert "update_code" in lam.calls


def test_ensure_failure_is_graceful_empty(fakes, tmp_path):
    lam, iam = fakes
    (tmp_path / "litellm-pricing-lambda").mkdir()
    (tmp_path / "litellm-pricing-lambda/lambda_function.py").write_text("x=1")
    def boom(**kw):
        raise RuntimeError("AccessDenied")
    lam.create_function = boom
    assert pricinglambda.ensure(_cfg(), tmp_path) == ""


def test_ensure_missing_source_returns_empty(fakes, tmp_path):
    assert pricinglambda.ensure(_cfg(), tmp_path) == ""


def test_teardown_removes_in_order(fakes):
    lam, iam = fakes
    lam.functions["llm-gateway-dev-litellm-pricing"] = {}
    iam.roles["llm-gateway-dev-litellm-pricing"] = {}
    iam.inline[("llm-gateway-dev-admin-api",
                pricinglambda.INVOKE_POLICY)] = "{}"
    ok = pricinglambda.teardown(
        _cfg(),
        admin_role_arn="arn:aws:iam::123456789012:role/llm-gateway-dev-admin-api")
    assert ok
    assert not lam.functions
    assert not iam.roles
    assert not iam.inline


def test_teardown_idempotent_on_empty(fakes):
    assert pricinglambda.teardown(_cfg()) is True
