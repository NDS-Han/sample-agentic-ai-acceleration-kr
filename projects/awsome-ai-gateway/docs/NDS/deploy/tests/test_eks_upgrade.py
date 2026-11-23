# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks_upgrade + infra.eks_version — 버전 파싱·홉 계산·tfvars 갱신·검증."""
import json
import subprocess

import pytest

from deploy import eks_apply, eks_upgrade, schema


def _eks_doc(**over):
    doc = {"version": 1, "env": "acme-test",
           "deploy": {"target": "eks", "size_tier": "t3"},
           "images": {"tag": "abc123"}}
    doc.update(over)
    return doc


class TestMinorAndHops:
    def test_minor_valid(self):
        assert eks_upgrade._minor("1.34") == 34
        assert eks_upgrade._minor("1.31") == 31
        assert eks_upgrade._minor(" 1.34 ") == 34

    @pytest.mark.parametrize("bad", ["", "1", "v1.34", "1.34.1", "2.1",
                                     "1.x", "abc"])
    def test_minor_rejects_malformed_str(self, bad):
        with pytest.raises(eks_upgrade.UpgradeError):
            eks_upgrade._minor(bad)

    @pytest.mark.parametrize("bad", [1.34, 134, None])
    def test_minor_rejects_non_str(self, bad):
        # float 1.30 을 str() 하면 "1.3" 으로 minor 가 왜곡된다 — 타입으로 거절
        with pytest.raises(eks_upgrade.UpgradeError):
            eks_upgrade._minor(bad)

    def test_hops_multi_minor(self):
        assert eks_upgrade._hops(31, 34) == [32, 33, 34]

    def test_hops_single(self):
        assert eks_upgrade._hops(33, 34) == [34]

    def test_hops_same_version_raises(self):
        with pytest.raises(eks_upgrade.UpgradeError, match="같습니다"):
            eks_upgrade._hops(34, 34)

    def test_hops_downgrade_rejected(self):
        with pytest.raises(eks_upgrade.UpgradeError, match="다운그레이드"):
            eks_upgrade._hops(34, 31)


class TestAddonDefaults:
    def _addons(self, versions, default_cluster="1.34"):
        """describe-addon-versions 응답 형태 — 마지막 항목만 defaultVersion."""
        return {"addons": [{
            "addonName": name,
            "addonVersions": [
                {"addonVersion": v,
                 "compatibilities": [
                     {"clusterVersion": default_cluster,
                      "defaultVersion": i == len(vs) - 1}]}
                for i, v in enumerate(vs)]
            } for name, vs in versions.items()]}

    def _three(self, **over):
        base = {"coredns": ["v1.12.1-eksbuild.1"],
                "kube-proxy": ["v1.34.0-eksbuild.1"],
                "vpc-cni": ["v1.20.1-eksbuild.1"]}
        base.update(over)
        return base

    def test_picks_default_version(self, monkeypatch):
        monkeypatch.setattr(
            eks_upgrade, "_aws_json", lambda *a, **k: self._addons({
                "coredns": ["v1.12.1-eksbuild.1"],
                "kube-proxy": ["v1.12.0-eksbuild.2",
                               "v1.12.3-eksbuild.1"],
                "vpc-cni": ["v1.20.1-eksbuild.1"]}))
        got = eks_upgrade._addon_defaults("1.34", "ap-south-1")
        assert got["kube_proxy"] == "v1.12.3-eksbuild.1"

    def test_maps_all_addons(self, monkeypatch):
        monkeypatch.setattr(
            eks_upgrade, "_aws_json",
            lambda *a, **k: self._addons(self._three()))
        got = eks_upgrade._addon_defaults("1.34", "r")
        assert got == {"coredns": "v1.12.1-eksbuild.1",
                       "kube_proxy": "v1.34.0-eksbuild.1",
                       "vpc_cni": "v1.20.1-eksbuild.1"}

    def test_current_compatible_is_kept(self, monkeypatch):
        # 운영자가 기본값보다 높은 핀을 뒀는데 그 버전이 목표 k8s 호환이면
        # 그대로 유지 — 불필요한 애드온 롤링을 안 한다.
        monkeypatch.setattr(
            eks_upgrade, "_aws_json",
            lambda *a, **k: self._addons(
                self._three(**{"vpc-cni": ["v1.20.1-eksbuild.1",
                                           "v1.22.4-eksbuild.14"]})))
        got = eks_upgrade._addon_defaults(
            "1.34", "r", current={"vpc_cni": "v1.22.4-eksbuild.14"})
        assert got["vpc_cni"] == "v1.22.4-eksbuild.14"

    def test_higher_incompatible_current_refused(self, monkeypatch):
        # 현재가 기본값보다 높은데 호환 목록에 없으면 다운그레이드를 자동
        # 시도하지 않고 중단한다.
        monkeypatch.setattr(
            eks_upgrade, "_aws_json",
            lambda *a, **k: self._addons(self._three()))
        with pytest.raises(eks_upgrade.UpgradeError, match="vpc_cni"):
            eks_upgrade._addon_defaults(
                "1.34", "r", current={"vpc_cni": "v9.99.9-eksbuild.1"})

    def test_lower_incompatible_current_uses_default(self, monkeypatch):
        # 현재가 비호환이고 기본값이 더 새 버전이면 기본값으로 전진한다.
        monkeypatch.setattr(
            eks_upgrade, "_aws_json",
            lambda *a, **k: self._addons(
                self._three(**{"vpc-cni": ["v1.19.0-eksbuild.1",
                                           "v1.20.1-eksbuild.1"]})))
        got = eks_upgrade._addon_defaults(
            "1.34", "r", current={"vpc_cni": "v1.18.3-eksbuild.1"})
        assert got["vpc_cni"] == "v1.20.1-eksbuild.1"

    def test_missing_addon_fails_closed(self, monkeypatch):
        # 기본값을 못 찾은 채 tfvars 를 쓰면 variables.tf 낡은 핀이 적용돼
        # skew 가 생긴다 — 부분 결과로 진행하지 않고 멈춰야 한다.
        monkeypatch.setattr(
            eks_upgrade, "_aws_json",
            lambda *a, **k: self._addons({"coredns": ["v1-eksbuild.1"]}))
        with pytest.raises(eks_upgrade.UpgradeError,
                           match="kube_proxy"):
            eks_upgrade._addon_defaults("1.34", "r")

    def test_empty_result_raises(self, monkeypatch):
        monkeypatch.setattr(eks_upgrade, "_aws_json",
                            lambda *a, **k: {"addons": []})
        with pytest.raises(eks_upgrade.UpgradeError):
            eks_upgrade._addon_defaults("1.34", "r")


class TestStripMapKey:
    def test_multiline_block(self):
        text = 'eks_addon_versions = { # c\n  a = "1"\n}\nkeep = "x"\n'
        out = eks_upgrade._strip_map_key(text, "eks_addon_versions")
        assert "eks_addon_versions" not in out and 'keep = "x"' in out

    def test_single_line_map(self):
        # 한 줄 맵 — 정규식 ^} 앵커로는 못 지우는 형태
        text = 'eks_addon_versions = { a = "1" }\nkeep = "x"\n'
        out = eks_upgrade._strip_map_key(text, "eks_addon_versions")
        assert "eks_addon_versions" not in out and 'keep = "x"' in out

    def test_nested_braces_and_following_block(self):
        text = ('eks_addon_versions = { a = "1" }\n'
                'tags = { env = "dev" }\n')
        out = eks_upgrade._strip_map_key(text, "eks_addon_versions")
        assert 'tags = { env = "dev" }' in out

    def test_unclosed_raises(self):
        with pytest.raises(eks_upgrade.UpgradeError):
            eks_upgrade._strip_map_key('eks_addon_versions = { a = "1"\n',
                                       "eks_addon_versions")


class TestWriteVersionTfvars:
    def test_missing_file_refused(self, tmp_path):
        # tfvars 부재 상태로 버전 키만 쓰면 access_entries 등 사람 소유
        # 값이 기본값으로 초기화된다 — 새 머신 함정 차단.
        with pytest.raises(eks_upgrade.UpgradeError, match="terraform.tfvars"):
            eks_upgrade._write_version_tfvars(tmp_path, "1.34", {})

    def test_rewrites_existing_keys(self, tmp_path):
        t = tmp_path / "terraform.tfvars"
        t.write_text('env = "dev"\n'
                     'eks_cluster_version = "1.31"\n'
                     'eks_addon_versions = { # comment\n'
                     '  coredns    = "v1.11.3-eksbuild.1"\n'
                     '  kube_proxy = "v1.29.7-eksbuild.2"\n'
                     '  vpc_cni    = "v1.18.3-eksbuild.1"\n'
                     '}\n'
                     'aurora_engine_version = "16.6"\n')
        eks_upgrade._write_version_tfvars(
            tmp_path, "1.32",
            {"coredns": "v2", "kube_proxy": "v3", "vpc_cni": "v4"})
        out = t.read_text()
        assert 'eks_cluster_version = "1.32"' in out
        assert '"1.31"' not in out
        assert 'coredns = "v2"' in out
        assert "v1.11.3" not in out
        # 무관 키 보존
        assert 'env = "dev"' in out and 'aurora_engine_version = "16.6"' in out
        assert out.count("eks_addon_versions") == 1
        # .bak 백업 생성
        assert (tmp_path / "terraform.tfvars.bak").exists()

    def test_no_trailing_newline_and_indented(self, tmp_path):
        t = tmp_path / "terraform.tfvars"
        t.write_text('  eks_cluster_version = "1.31"')  # 개행 없음·들여쓰기
        eks_upgrade._write_version_tfvars(tmp_path, "1.34", {})
        out = t.read_text()
        # 중복 정의가 되면 안 된다 — 기존 할당이 지워지고 새 값만 남는다
        assert out.count("eks_cluster_version") == 1
        assert '"1.34"' in out

    def test_appends_when_key_missing(self, tmp_path):
        t = tmp_path / "terraform.tfvars"
        t.write_text('env = "dev"\n')
        eks_upgrade._write_version_tfvars(
            tmp_path, "1.34",
            {"coredns": "c", "kube_proxy": "k", "vpc_cni": "v"})
        out = t.read_text()
        assert 'eks_cluster_version = "1.34"' in out
        assert "eks_addon_versions = {" in out
        assert 'env = "dev"' in out


class TestInspectUpgradePlan:
    def _show(self, changes):
        return subprocess.CompletedProcess(
            [], 0, json.dumps({"resource_changes": changes}), "")

    def _rc(self, addr, actions, before_v="", after_v=""):
        ch = {"actions": actions}
        if "aws_eks_cluster" in addr:
            ch["before"] = {"version": before_v}
            ch["after"] = {"version": after_v}
        return {"mode": "managed", "address": addr, "change": ch}

    def test_out_of_scope_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            eks_upgrade, "_run_quiet",
            lambda *a, **k: self._show([
                self._rc("module.eks.aws_eks_cluster.this[0]",
                         ["update"], "1.34", "1.35"),
                self._rc("module.vpc.aws_subnet.x", ["update"])]))
        bad, changes, bump = eks_upgrade._inspect_upgrade_plan(
            tmp_path, tmp_path / "p", "1.35")
        assert any("밖" in b for b in bad) and bump

    def test_delete_blocked(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            eks_upgrade, "_run_quiet",
            lambda *a, **k: self._show([
                self._rc("module.eks.aws_eks_fargate_profile.x",
                         ["delete", "create"])]))
        bad, changes, _ = eks_upgrade._inspect_upgrade_plan(
            tmp_path, tmp_path / "p", "1.35")
        assert any("파괴" in b for b in bad)

    def test_time_sleep_replace_allowed(self, monkeypatch, tmp_path):
        # terraform-aws-eks 의 데이터플레인 대기 리소스는 버전 변경마다
        # 항상 create+delete — 상태 없는 sleep 이라 예외로 통과시킨다.
        monkeypatch.setattr(
            eks_upgrade, "_run_quiet",
            lambda *a, **k: self._show([
                self._rc("module.eks.module.eks.aws_eks_cluster.this[0]",
                         ["update"], "1.34", "1.35"),
                self._rc("module.eks.module.eks.time_sleep.this[0]",
                         ["create", "delete"])]))
        bad, changes, bump = eks_upgrade._inspect_upgrade_plan(
            tmp_path, tmp_path / "p", "1.35")
        assert not bad and bump and len(changes) == 2

    def test_version_bump_detected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            eks_upgrade, "_run_quiet",
            lambda *a, **k: self._show([
                self._rc("module.eks.aws_eks_cluster.this[0]",
                         ["update"], "1.34", "1.35"),
                self._rc("module.eks.aws_eks_addon.a", ["update"])]))
        bad, changes, bump = eks_upgrade._inspect_upgrade_plan(
            tmp_path, tmp_path / "p", "1.35")
        assert bump and not bad and len(changes) == 2

    def test_noop_filtered(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            eks_upgrade, "_run_quiet",
            lambda *a, **k: self._show([
                self._rc("module.eks.aws_eks_cluster.this[0]", ["no-op"])]))
        bad, changes, bump = eks_upgrade._inspect_upgrade_plan(
            tmp_path, tmp_path / "p", "1.35")
        assert not (bad or changes or bump)


class TestVerifyContext:
    def test_mismatch_blocked(self, monkeypatch):
        monkeypatch.setattr(eks_upgrade, "_kube_context_cluster",
                            lambda c: "other-cluster")
        with pytest.raises(eks_upgrade.UpgradeError, match="다른 클러스터"):
            eks_upgrade._verify_context("ctx", "llm-gateway-dev")

    def test_match_ok(self, monkeypatch):
        monkeypatch.setattr(eks_upgrade, "_kube_context_cluster",
                            lambda c: "llm-gateway-dev")
        eks_upgrade._verify_context("", "llm-gateway-dev")  # 예외 없음

    def test_unresolved_warns_not_blocks(self, monkeypatch, capsys):
        monkeypatch.setattr(eks_upgrade, "_kube_context_cluster",
                            lambda c: "")
        eks_upgrade._verify_context("", "llm-gateway-dev")
        assert "⚠" in capsys.readouterr().err


class TestSchemaEksVersion:
    def test_valid_versions(self):
        for v in ("1.31", "1.32", "1.34"):
            cfg = schema.from_dict(
                _eks_doc(infra={"eks_version": v}))
            assert cfg.infra.eks_version == v
            assert not cfg.validate()

    @pytest.mark.parametrize("bad", ["1", "v1.34", "1.34.1", "134"])
    def test_rejects_malformed(self, bad):
        with pytest.raises(schema.SchemaError, match="eks_version"):
            schema.from_dict(_eks_doc(infra={"eks_version": bad}))

    @pytest.mark.parametrize("bad", [1.34, 1.30])
    def test_rejects_float_yaml(self, bad):
        # 따옴표 없는 숫자는 yaml 이 float 으로 읽는다 — 1.30 → 1.3 왜곡 방지
        with pytest.raises(schema.SchemaError, match="eks_version"):
            schema.from_dict(_eks_doc(infra={"eks_version": bad}))

    def test_warns_on_compose_target(self):
        cfg = schema.from_dict({
            "version": 1, "env": "acme-test",
            "deploy": {"target": "compose", "size_tier": "t0"},
            "infra": {"eks_version": "1.34"}})
        assert any("eks 전용" in w for w in cfg.warnings())


class TestWarnVersionDrift:
    def _cfg(self, v):
        return schema.from_dict(_eks_doc(infra={"eks_version": v}))

    def test_warns_on_mismatch(self, monkeypatch, capsys):
        import deploy.capture as cap
        monkeypatch.setattr(
            cap, "_aws_json",
            lambda *a, **k: {"cluster": {"version": "1.33"}})
        eks_apply._warn_version_drift(
            self._cfg("1.34"), {"cluster_name": "c"})
        assert "1.33" in capsys.readouterr().err

    def test_silent_on_match(self, monkeypatch, capsys):
        import deploy.capture as cap
        monkeypatch.setattr(
            cap, "_aws_json",
            lambda *a, **k: {"cluster": {"version": "1.34"}})
        eks_apply._warn_version_drift(
            self._cfg("1.34"), {"cluster_name": "c"})
        assert capsys.readouterr().err == ""

    def test_noop_without_keys(self, capsys):
        eks_apply._warn_version_drift(self._cfg("1.34"), {})
        assert capsys.readouterr().err == ""

    def test_aws_failure_ignored(self, monkeypatch, capsys):
        import deploy.capture as cap
        def _boom(*a, **k):
            raise RuntimeError("aws down")
        monkeypatch.setattr(cap, "_aws_json", _boom)
        eks_apply._warn_version_drift(
            self._cfg("1.34"), {"cluster_name": "c"})
        assert capsys.readouterr().err == ""
