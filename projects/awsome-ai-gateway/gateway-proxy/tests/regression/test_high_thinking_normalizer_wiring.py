# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""`thinking` 정규화와 도구 필터가 **실제로 배선돼 있는지**.

무엇이 문제였나
---------------
``services/thinking_normalizer.py`` 와 ``services/tool_filter.py`` 는 완성돼 있고 단위
테스트도 통과하는데, ``src/`` 의 어느 코드도 두 모듈을 import 하지 않았다 — 죽은 모듈이다.
그 사이 두 방향의 하드 400 이 열려 있었다:

  * ``claude-opus-4-7 / 4-8 / opus-5 / sonnet-5 / fable-5 / mythos-5`` 는
    ``{"type":"adaptive"}`` 만 받고 ``enabled`` 를 거부한다. ``MAX_THINKING_TOKENS>0`` 로
    설정된 Claude Code 는 ``enabled`` 를 보낸다 → 그 클라이언트의 **모든** 요청이 400.
    400 은 ``_FALLBACK_STATUSES`` 가 아니라 폴백도 재시도도 없이 그대로 사용자에게 간다.
  * ``claude-haiku-4-5`` 는 그 반대로 ``adaptive`` 를 거부한다.

세 번째 경로는 클라이언트 변경 없이도 열린다: 예산 강등이 임계값에서 계열을 넘어 모델을
바꾸므로, 예산 79% 에서 되던 요청이 80% 에서 400 이 된다.

Mantle 쪽은 도구다: Cowork 가 Anthropic **네이티브** 도구
(``web_search_20250305`` / ``code_execution_*`` / ``text_editor_*`` / ``computer_*``)를
선언하면 "tool type is not supported for this model" 로 400 이다.

그리고 반대 방향의 조용한 손실이 두 곳 있었다: 폴백 루프와 강등 미들웨어가 haiku 후보의
``thinking`` 을 **무조건 지웠다.** haiku 는 ``enabled`` 를 실제로 받으므로, 사용자는
HTTP 200 을 받으면서 extended thinking 만 잃었다 — 실패가 아니라 기능 상실이라 아무도
눈치채지 못한다.

⚠️ 배선 검사는 **본문을 만들어 보고** 한다. 이 파일의 대상은 "import 했는가" 가 아니라
   "상류로 나가는 바이트가 계열에 맞는가" 다.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.services.thinking_normalizer import _family_from_alias, normalize_thinking
from app.services.tool_filter import strip_unsupported_tools

_SRC = Path(__file__).resolve().parents[2] / "src" / "app"


# ─────────────────────────────────────────────────────────────────────────────
# 1. 두 계열의 변환
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "pmid",
    [
        "us.anthropic.claude-opus-4-7",
        "us.anthropic.claude-opus-4-8",
        "global.anthropic.claude-opus-5",
        "anthropic.claude-sonnet-5",
    ],
)
def test_enabled_becomes_adaptive_for_the_adaptive_only_family(pmid: str):
    """⚠️ 이 변환이 없으면 그 클라이언트의 **모든** 요청이 400 이다."""
    out = normalize_thinking(
        {"thinking": {"type": "enabled", "budget_tokens": 8000}, "max_tokens": 16000}, pmid
    )
    assert out["thinking"] == {"type": "adaptive"}, f"{pmid}: {out['thinking']}"


def test_adaptive_becomes_enabled_for_the_legacy_family():
    out = normalize_thinking(
        {"thinking": {"type": "adaptive"}, "max_tokens": 16000},
        "us.anthropic.claude-haiku-4-5-20251001",
    )
    assert out["thinking"]["type"] == "enabled"
    assert out["thinking"]["budget_tokens"] >= 1024


def test_an_already_correct_shape_is_left_alone():
    """⚠️ 이것이 옛 결함의 반대편이다.

    haiku 는 ``enabled`` 를 실제로 받는다. 예전 코드는 그것까지 지워서 사용자에게 200 을
    주면서 extended thinking 만 없앴다 — 실패가 아니라 조용한 기능 상실이다.
    """
    body = {"thinking": {"type": "enabled", "budget_tokens": 1000}, "max_tokens": 4000}
    out = normalize_thinking(dict(body), "us.anthropic.claude-haiku-4-5-20251001")
    assert out["thinking"] == body["thinking"], f"유효한 형태가 바뀌었다: {out['thinking']}"


def test_an_unclassified_model_is_untouched():
    """⚠️ 미분류 모델은 건드리지 않는다(fail-open).

    sonnet-4-6 / opus-4-6 은 **의도적으로** 두 목록 어디에도 없다 — 실측 결과 `enabled` 를
    받아들이기 때문이다. 접두사 목록에 넣으면 되던 요청을 우리가 고장낸다.
    """
    body = {"thinking": {"type": "enabled", "budget_tokens": 8000}, "max_tokens": 16000}
    out = normalize_thinking(dict(body), "us.anthropic.claude-sonnet-4-6")
    assert out == body


def test_the_geo_prefix_does_not_hide_the_family():
    for geo in ("us.", "eu.", "apac.", "global."):
        out = normalize_thinking(
            {"thinking": {"type": "enabled"}, "max_tokens": 9000},
            f"{geo}anthropic.claude-opus-4-8",
        )
        assert out["thinking"] == {"type": "adaptive"}, geo


def test_a_malformed_thinking_block_is_passed_through():
    """정규화 실패로 요청을 깨뜨리지 않는다."""
    for bad in (None, "enabled", 42, {"type": "something-else"}):
        body = {"thinking": bad, "max_tokens": 100}
        assert normalize_thinking(dict(body), "us.anthropic.claude-opus-4-8") == body


# ─────────────────────────────────────────────────────────────────────────────
# 2. alias 기반 계열 추정 (강등 미들웨어용)
# ─────────────────────────────────────────────────────────────────────────────


def test_an_operator_named_alias_is_still_classified():
    """⚠️ 강등 계층은 모델 **해석 전**이라 alias 만 안다.

    예전에는 ``startswith("claude-haiku-4-5")`` 로 판정해서, 운영자가 만든
    ``team-haiku-cheap`` 같은 alias 는 판정을 빠져나가 강등 뒤 400 이 됐다.
    """
    assert _family_from_alias("team-haiku-cheap") == "legacy"
    assert _family_from_alias("my-opus-4-8-fast") == "adaptive"
    assert _family_from_alias("claude-sonnet-4-6") is None
    assert _family_from_alias(None) is None


def test_normalize_thinking_actually_uses_the_alias_fallback():
    """⚠️ ``_family_from_alias`` 가 존재하는 것과 ``normalize_thinking`` 이 그것을 쓰는 것은
    다른 주장이다.

    처음에 이 파일은 헬퍼를 직접만 호출해서, 정규화 쪽의 폴백 사용을 지워도 통과했다
    (대조군으로 확인). 그래서 **pmid 를 모르는 상태**로 정규화를 돌려 결과를 본다 — 강등
    미들웨어가 실제로 있는 상황이 그것이다.
    """
    out = normalize_thinking(
        {"thinking": {"type": "adaptive"}, "max_tokens": 16000},
        None,                      # 해석 전 계층 — provider_model_id 를 모른다
        alias="team-haiku-cheap",  # 운영자가 지은 이름
    )
    assert out["thinking"]["type"] == "enabled", (
        f"alias 폴백이 쓰이지 않았다: {out['thinking']} — 강등 뒤 haiku 요청이 400 이 된다"
    )

    out2 = normalize_thinking(
        {"thinking": {"type": "enabled", "budget_tokens": 8000}, "max_tokens": 16000},
        None,
        alias="my-opus-4-8-fast",
    )
    assert out2["thinking"] == {"type": "adaptive"}, (
        f"adaptive 계열 alias 폴백이 쓰이지 않았다: {out2['thinking']}"
    )


def test_the_alias_fallback_only_applies_when_the_model_id_is_unknown():
    """provider_model_id 를 알면 그것이 우선이어야 한다 — alias 는 운영자가 짓는 이름이다."""
    out = normalize_thinking(
        {"thinking": {"type": "enabled"}, "max_tokens": 9000},
        "us.anthropic.claude-opus-4-8",   # adaptive 계열
        alias="team-haiku-cheap",         # 이름만 haiku
    )
    assert out["thinking"] == {"type": "adaptive"}, (
        "alias 가 provider_model_id 를 덮었다 — 실제 모델이 판정 기준이어야 한다"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3. output_config 는 한 쌍으로만 성립한다
# ─────────────────────────────────────────────────────────────────────────────


def test_output_config_is_allowed_through_to_the_adaptive_family():
    """adaptive 계열이 thinking 깊이를 제어하는 유일한 수단이다."""
    src = (_SRC / "routers" / "messages.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_BEDROCK_ALLOWED_FIELDS" for t in node.targets
        ):
            allowed = {e.value for e in node.value.elts if isinstance(e, ast.Constant)}
    assert allowed is not None, "_BEDROCK_ALLOWED_FIELDS 를 찾지 못했다"
    assert "output_config" in allowed, (
        "output_config 가 허용 필드에 없다 — 클라이언트가 보내도 우리가 버려서 "
        "adaptive 계열의 깊이 제어가 동작하지 않는다"
    )


def test_the_legacy_family_always_loses_output_config():
    """⚠️ 위 허용과 **한 쌍**이다. 한쪽만 하면 haiku 가 400 을 내기 시작한다.

    haiku 는 output_config 를 받지 않는다. thinking 이 이미 ``enabled`` 라 변환이 필요
    없는 경우에도 이 필드는 떨궈야 한다 — 그 경로가 정확히 빠지기 쉬운 곳이다.
    """
    out = normalize_thinking(
        {
            "thinking": {"type": "enabled", "budget_tokens": 1000},
            "output_config": {"effort": "high"},
            "max_tokens": 4000,
        },
        "us.anthropic.claude-haiku-4-5-20251001",
    )
    assert "output_config" not in out, f"legacy 계열에 output_config 가 남았다: {out}"
    assert out["thinking"]["type"] == "enabled", "유효한 thinking 까지 지웠다"


def test_the_adaptive_family_keeps_output_config():
    out = normalize_thinking(
        {
            "thinking": {"type": "enabled"},
            "output_config": {"effort": "high"},
            "max_tokens": 9000,
        },
        "us.anthropic.claude-opus-4-8",
    )
    assert out["output_config"] == {"effort": "high"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. 도구 필터
# ─────────────────────────────────────────────────────────────────────────────


def test_native_anthropic_tools_are_stripped():
    body = {
        "tools": [
            {"type": "web_search_20250305", "name": "web_search"},
            {"type": "code_execution_20250522", "name": "code"},
            {"name": "my_custom_tool", "input_schema": {}},
        ]
    }
    out = strip_unsupported_tools(dict(body))
    kept = [t.get("name") for t in out["tools"]]
    assert kept == ["my_custom_tool"], f"필터 결과: {kept}"


def test_our_injected_web_search_survives_the_filter():
    """⚠️ 우리 서버사이드 web_search 는 걸러지면 안 된다.

    필터는 ``type`` 필드로 판정하고, 주입된 도구에는 그 필드가 없다 — 그 성질이 깨지면
    웹서치 기능이 통째로 죽는다.
    """
    from app.services.web_search_loop import GW_WEB_SEARCH_NAME

    body = {
        "tools": [
            {"name": GW_WEB_SEARCH_NAME, "description": "search", "input_schema": {}},
            {"type": "web_search_20250305", "name": "web_search_native"},
        ]
    }
    out = strip_unsupported_tools(dict(body))
    kept = [t.get("name") for t in out["tools"]]
    assert GW_WEB_SEARCH_NAME in kept, f"주입한 도구가 걸러졌다: {kept}"
    assert "web_search_native" not in kept


# ─────────────────────────────────────────────────────────────────────────────
# 5. 배선 — 죽은 모듈이 되지 않는가
# ─────────────────────────────────────────────────────────────────────────────


def _tree(rel: str) -> ast.Module:
    src = (_SRC / rel).read_text(encoding="utf-8")
    assert len(src) > 1000, f"{rel} 가 너무 짧다 — 경로 확인"
    return ast.parse(src)


def _calls(node, name: str) -> list[int]:
    return [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_both_modules_are_called_from_production_code():
    """⚠️ 두 모듈은 완성돼 있고 단위 테스트도 통과하면서 **아무도 부르지 않았다.**

    통과하는 단위 테스트가 죽은 모듈에 보증서를 붙이고 있었다.
    """
    tree = _tree("routers/messages.py")
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_candidate_body"
    )
    assert _calls(fn, "normalize_thinking"), "_build_candidate_body 가 정규화를 부르지 않는다"
    assert _calls(fn, "strip_unsupported_tools"), (
        "_build_candidate_body 가 도구 필터를 부르지 않는다"
    )


def test_normalization_happens_on_both_provider_branches():
    """Mantle 과 Bedrock 분기 **둘 다**여야 한다.

    한쪽만 하면 그 provider 의 클라이언트만 400 이고, 재현 조건이 좁아 오래 남는다.
    """
    tree = _tree("routers/messages.py")
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_build_candidate_body"
    )
    assert len(_calls(fn, "normalize_thinking")) >= 2, (
        f"정규화가 {len(_calls(fn, 'normalize_thinking'))}곳뿐이다 — mantle/bedrock 두 분기 필요"
    )
    assert len(_calls(fn, "strip_unsupported_tools")) >= 2


def test_the_unconditional_haiku_strip_is_gone():
    """⚠️ 무조건 제거가 되살아나면 사용자는 200 을 받으면서 thinking 을 잃는다."""
    for rel in ("services/fallback_loop.py", "middleware/downgrade.py"):
        src = (_SRC / rel).read_text(encoding="utf-8")
        code = "\n".join(
            line for line in src.split("\n") if not line.strip().startswith("#")
        )
        assert 'startswith("claude-haiku-4-5")' not in code, (
            f"{rel}: 접두사 기반 haiku 판정이 남아 있다 — 운영자 alias 를 놓친다"
        )
        assert 'pop("thinking")' not in code, (
            f"{rel}: thinking 을 무조건 제거한다 — haiku 가 받는 형태까지 지운다"
        )


def test_the_downgrade_layer_normalizes_by_alias():
    """강등 계층은 provider_model_id 를 모른다 — alias 로 넘겨야 한다."""
    tree = _tree("middleware/downgrade.py")
    found = False
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "normalize_thinking"
        ):
            kwargs = {k.arg for k in node.keywords}
            assert "alias" in kwargs, f"L{node.lineno}: alias 를 넘기지 않는다"
            found = True
    assert found, "강등 계층이 정규화를 부르지 않는다"


def test_the_body_logger_records_what_actually_went_on_the_wire():
    """⚠️ 알려진 격차를 명시적으로 고정한다.

    본문 로거는 초기 요청 본문(``body``)을 기록하고, 폴백/강등 후 실제로 상류에 나간 본문은
    ``_build_candidate_body`` 가 만든 것이다. 정규화가 후자에만 적용되므로, 감사 레코드의
    ``thinking`` 은 상류가 실제로 받은 것과 다를 수 있다.

    이 테스트는 그 사실을 **기록**한다 — 지금 고치려면 로깅 지점을 후보 본문으로 옮겨야
    하고, 그러면 폴백이 일어난 요청의 감사 레코드가 후보마다 달라진다(설계 결정이 필요하다).
    지금은 정규화가 상류 형태를 맞추는 것이 우선이고, 감사 쪽 차이는 알려진 상태로 둔다.
    """
    src = (_SRC / "routers" / "messages.py").read_text(encoding="utf-8")
    # 로깅이 여전히 초기 body 를 쓰는지 확인(사실 고정). 바뀌면 이 주석을 갱신할 것.
    assert "request_body=body," in src, (
        "본문 로깅 대상이 바뀌었다 — 이 테스트의 docstring 에 적힌 격차 설명을 갱신할 것"
    )
