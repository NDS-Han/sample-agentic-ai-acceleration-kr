# Copyright 2026 © Amazon.com and Affiliates.
"""eks_build — 변경분 감지·migration 게이트·yaml 핀 갱신의 순수 로직."""
from __future__ import annotations

import subprocess
import sys
import types

import pytest

from .. import eks_build


def _cp(rc=0, out="", err=""):
    return subprocess.CompletedProcess([], rc, out, err)


def _cfg(tags=None, tag="base1111"):
    img = types.SimpleNamespace(tags=tags or {}, tag=tag,
                                registry="123.dkr.ecr.ap-south-1.amazonaws.com")
    return types.SimpleNamespace(images=img,
                                 aws=types.SimpleNamespace(region="ap-south-1"))


class TestDetectRebuilds:
    """컨텍스트 diff 결과에 따른 서비스 선택 — git 호출은 목킹."""

    def _patch(self, monkeypatch, changed=None, commits=None):
        changed = changed or set()
        commits = commits or set()

        def fake(argv, *, cwd=None, input=None):
            if argv[:2] == ["git", "rev-parse"] and "--verify" in argv:
                ref = argv[-1].split("^")[0]
                return _cp(0 if ref in commits else 1)
            if argv[:2] == ["git", "diff"]:
                ctx = argv[-1].rstrip("/")
                return _cp(1 if ctx in changed else 0)
            return _cp(0)

        monkeypatch.setattr(eks_build, "_q", fake)

    def test_changed_service_only(self, monkeypatch):
        # admin-api ctx 만 변경 — adminApi 만 리빌드 대상
        commits = {"old11111", "base1111"}
        pins = {"adminApi": "old11111", "gatewayProxy": "old11111",
                "adminUi": "old11111", "notificationWorker": "old11111",
                "costRecorderWorker": "old11111", "migration": "old11111"}
        self._patch(monkeypatch, changed={"admin-api"}, commits=commits)
        got = eks_build.detect_rebuilds(_cfg(pins), __import__("pathlib").Path("."), False)
        assert set(got) == {"adminApi"}

    def test_unresolvable_pin_rebuilds(self, monkeypatch):
        # 핀이 git 개체가 아니면(템플릿 태그 등) 비교 불가 → 리빌드
        self._patch(monkeypatch, changed=set(), commits={"base1111"})
        got = eks_build.detect_rebuilds(
            _cfg({"adminApi": "1.0.4-tpl"}), __import__("pathlib").Path("."), False)
        assert "adminApi" in got

    def test_build_all_includes_everything(self, monkeypatch):
        self._patch(monkeypatch)
        got = eks_build.detect_rebuilds(_cfg(), __import__("pathlib").Path("."), True)
        assert set(got) == {k for k, _, _ in eks_build.EKS_SERVICES}


class TestMigrationGate:
    def test_new_revision_blocks_when_migration_not_built(self, monkeypatch):
        def fake(argv, *, cwd=None, input=None):
            if "--verify" in argv:
                return _cp(0)  # mig_pin 은 커밋
            if argv[:2] == ["git", "diff"]:
                return _cp(1)  # db/versions/ 에 새 파일 있음
            return _cp(0)
        monkeypatch.setattr(eks_build, "_q", fake)
        cfg = _cfg({"migration": "mig1111"})
        with pytest.raises(SystemExit):
            eks_build._migration_gate(cfg, __import__("pathlib").Path("."),
                                      {"adminApi": "x"})

    def test_migration_in_set_skips_gate(self, monkeypatch):
        monkeypatch.setattr(eks_build, "_q",
                            lambda *a, **k: (_ for _ in ()).throw(AssertionError))
        eks_build._migration_gate(_cfg(), __import__("pathlib").Path("."),
                                  {"migration": "x"})


class TestRewriteTags:
    SAMPLE = """\
images:
  tag: base1111
  registry: 123.dkr.ecr.ap-south-1.amazonaws.com
  tags:
    gatewayProxy: 14de8c07   # 26차 — 프록시 핀
    adminApi: f519b36b    # 33차 — api 핀
    migration: f519b36b    # 0057
infra:
  x: 1
"""

    def test_rewrites_existing_and_keeps_comment(self):
        out = eks_build.rewrite_tags(self.SAMPLE, {"adminApi": "d0496ed7"})
        assert 'adminApi: "d0496ed7"  # 33차 — api 핀' in out
        assert "gatewayProxy: 14de8c07" in out  # 다른 핀 불변
        assert "migration: f519b36b" in out

    def test_inserts_missing_key(self):
        out = eks_build.rewrite_tags(self.SAMPLE, {"adminUi": "d0496ed7",
                                                   "scheduler": "d0496ed7"})
        assert 'adminUi: "d0496ed7"' in out
        assert 'scheduler: "d0496ed7"' in out
        # tags: 블록 안쪽에 삽입됨 — infra: 보다 앞
        assert out.index('adminUi: "d0496ed7"') < out.index("infra:")
