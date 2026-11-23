# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""eks_upgrade + infra.eks_version — 버전 파싱·홉 계산·tfvars 갱신·검증."""
from pathlib import Path

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

    def test_picks_default_version(self, monkeypatch):
        monkeypatch.setattr(
            eks_upgrade, "_aws_json", lambda *a, **k: self._addons({
                "coredns": ["v1.12.1-eksbuild.1"],
                # 비기본 버전과 기본 버전이 섞여 있으면 기본만 고른다
                "kube-proxy": ["v1.12.0-eksbuild.2",
                               "v1.12.3-eksbuild.1"],
                "vpc-cni": ["v1.20.1-eksbuild.1"]}))
        got = eks_upgrade._addon_defaults("1.34", "ap-south-1")
        assert got["kube_proxy"] == "v1.12.3-eksbuild.1"

    def test_maps_all_addons(self, monkeypatch):
        monkeypatch.setattr(
            eks_upgrade, "_aws_json",
            lambda *a, **k: self._addons({
                "coredns": ["v1.12.1-eksbuild.1"],
                "kube-proxy": ["v1.34.0-eksbuild.1"],
                "vpc-cni": ["v1.20.1-eksbuild.1"]}))
        got = eks_upgrade._addon_defaults("1.34", "r")
        assert got == {"coredns": "v1.12.1-eksbuild.1",
                       "kube_proxy": "v1.34.0-eksbuild.1",
                       "vpc_cni": "v1.20.1-eksbuild.1"}

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


class TestWriteVersionTfvars:
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
        # 애드온 블록은 통째 교체 + 주석으로 출처 명시
        assert 'coredns = "v2"' in out
        assert "v1.11.3" not in out
        # 무관 키 보존
        assert 'env = "dev"' in out and 'aurora_engine_version = "16.6"' in out
        # 블록이 한 번만 존재
        assert out.count("eks_addon_versions") == 1

    def test_appends_when_missing(self, tmp_path):
        t = tmp_path / "terraform.tfvars"
        t.write_text('env = "dev"\n')
        eks_upgrade._write_version_tfvars(
            tmp_path, "1.34",
            {"coredns": "c", "kube_proxy": "k", "vpc_cni": "v"})
        out = t.read_text()
        assert 'eks_cluster_version = "1.34"' in out
        assert "eks_addon_versions = {" in out
        assert 'env = "dev"' in out


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
