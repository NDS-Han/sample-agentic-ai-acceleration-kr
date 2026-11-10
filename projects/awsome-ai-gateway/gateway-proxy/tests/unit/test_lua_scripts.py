# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.lua_loader import LuaScriptLoader


def test_load_all_scripts():
    script_dir = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"
    if not script_dir.exists():
        pytest.skip("redis_scripts directory not found")

    LuaScriptLoader.load_all(script_dir)
    assert LuaScriptLoader.is_loaded("rate_limit_check")
    assert LuaScriptLoader.is_loaded("rate_limit_tpm_check")
    assert LuaScriptLoader.is_loaded("budget_check")
    assert LuaScriptLoader.is_loaded("budget_deduct")
    assert LuaScriptLoader.is_loaded("budget_reserve")
    assert LuaScriptLoader.is_loaded("budget_settle")
    assert LuaScriptLoader.is_loaded("budget_rebuild")


def test_get_script_not_loaded():
    with pytest.raises(KeyError):
        LuaScriptLoader.get("nonexistent_script")


def test_scripts_contain_keys_argv():
    script_dir = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"
    if not script_dir.exists():
        pytest.skip("redis_scripts directory not found")

    LuaScriptLoader.load_all(script_dir)
    # budget_check.lua uses only KEYS (no ARGV); others use both
    for name in ["rate_limit_check", "rate_limit_tpm_check", "budget_deduct"]:
        script = LuaScriptLoader.get(name)
        assert "KEYS" in script
        assert "ARGV" in script


def test_request_id_dedup_guards_present():
    """비멱등 eval의 서버측 멱등 보호 — 클라이언트 레벨 재시도 잔여 노출(M1) 차단."""
    script_dir = Path(__file__).parent.parent.parent / "src" / "app" / "redis_scripts"
    if not script_dir.exists():
        pytest.skip("redis_scripts directory not found")

    deduct = (script_dir / "budget_deduct.lua").read_text()
    reserve = (script_dir / "budget_reserve.lua").read_text()

    # deduct: 선택적 dedup 키(KEYS[3]) — 구버전 호출(2키)과 롤링배포 양방향 호환
    assert "KEYS[3]" in deduct
    assert "dedup_hit" in deduct
    # 마커는 쓰기 완료 후에 세운다 — 중간 크래시가 '마커만 있는' 상태를 만들지 않게
    assert deduct.index("EXISTS', dedup_key") < deduct.index("INCRBYFLOAT")

    # reserve: request_id 마커가 곧 멱등 키 — 재실행은 EXISTS 가드로 no-op 재생
    assert "EXISTS', marker_key" in reserve
    assert "dedup_hit" in reserve
