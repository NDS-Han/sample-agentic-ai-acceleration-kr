"""The web-search DEFAULTS are the measured, verified values — and there is only one set.

Until 1.0.79 the good values lived in an ops script (17-set-websearch-caps.sh) while the code
defaulted to 5 rounds / 10 results / 60000 chars / 4 per turn, text traces, and the client-tool
fixes off. An install that never ran the script paid the 2026-09-16 bill ($3.62 for one
question) and showed every defect that had already been fixed. Two sets of defaults also had
to be explained side by side in the beginner's guide.

Contract:
- ``Settings()`` with no environment gives the verified values (a change here is deliberate:
  re-measure first — see the comments next to the fields);
- the ops script's defaults are the same values, so "unset in config.env" and "unset in the
  pod" mean the same thing;
- every one of them is still overridable by env (rollback without a rebuild).
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import Settings

EXPECTED = {
    "web_search_max_searches_per_turn": 3,
    "web_search_max_iterations": 2,
    "web_search_max_results_default": 5,
    "web_search_max_result_chars": 12000,
    "web_search_trace_mode": "native",
    "web_search_trace_native_clients": "cowork,claude-code",
    "web_search_mixed_turn_run": True,
    "web_search_final_turn_soft": True,
}
_SCRIPT = (Path(__file__).resolve().parents[3] / "docs" / "us-llm-gateway" / "update-scripts"
           / "17-set-websearch-caps.sh")


def _clean_env(monkeypatch):
    for key in EXPECTED:
        monkeypatch.delenv(key.upper(), raising=False)


def test_code_defaults_are_the_verified_values(monkeypatch):
    _clean_env(monkeypatch)
    s = Settings(_env_file=None)
    assert {k: getattr(s, k) for k in EXPECTED} == EXPECTED


def test_ops_script_defaults_equal_the_code_defaults():
    text = _SCRIPT.read_text()
    script = dict(re.findall(r'^: "\$\{(WEB_SEARCH_[A-Z_]+):=([^}]*)\}"', text, flags=re.M))
    shown = dict(re.findall(r"\[(WEB_SEARCH_[A-Z_]+)\]=([^\s)]+)", text))
    for key, value in EXPECTED.items():
        env = key.upper()
        want = str(int(value)) if isinstance(value, bool) else str(value)
        assert script[env] == want, (
            f"{env}: script default {script[env]!r} != code default {want!r}")
        assert shown[env] == want, f"{env}: CODE_DEFAULT shown by the script is stale"


def test_every_default_can_still_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_TRACE_MODE", "text")
    monkeypatch.setenv("WEB_SEARCH_MIXED_TURN_RUN", "0")
    monkeypatch.setenv("WEB_SEARCH_FINAL_TURN_SOFT", "0")
    monkeypatch.setenv("WEB_SEARCH_MAX_ITERATIONS", "5")
    s = Settings(_env_file=None)
    assert (s.web_search_trace_mode, s.web_search_mixed_turn_run,
            s.web_search_final_turn_soft, s.web_search_max_iterations) == ("text", False, False, 5)
