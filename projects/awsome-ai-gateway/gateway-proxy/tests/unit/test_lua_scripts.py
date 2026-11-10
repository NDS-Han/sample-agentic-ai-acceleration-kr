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
    # 재생 가드(GET)는 차감 쓰기 전에, 마커 SET은 차감 쓰기 후에 와야 한다 —
    # 순서가 뒤집히면 스크립트 중간 런타임 오류가 '마커만 있고 차감 없는'
    # 상태를 만들어 재실행이 조용히 스킵된다(영구 과소계상).
    assert deduct.index("GET', dedup_key") < deduct.index("INCRBYFLOAT")
    assert deduct.index("SET', dedup_key") > deduct.index("INCRBYFLOAT")
    # 마커 값에 첫 실행의 threshold 교차 결과를 담는다 — 알림은 eval 응답에서
    # Python 이 발행하므로 응답 유실 재실행에서도 교차 이벤트가 살아있어야 한다.
    assert "'t:'" in deduct and "^t:" in deduct

    # reserve: request_id 마커가 곧 멱등 키 — 재실행 가드가 쓰기 전에 와야 한다
    assert "EXISTS', marker_key" in reserve
    assert "dedup_hit" in reserve
    assert reserve.index("EXISTS', marker_key") < reserve.index("INCRBYFLOAT")
