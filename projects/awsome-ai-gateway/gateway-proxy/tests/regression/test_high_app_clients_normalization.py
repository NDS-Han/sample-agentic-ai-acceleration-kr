# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""``app_clients`` 필드의 두 빈 형상(``[]`` 와 ``{}``)이 같게 해석되는지.

왜 두 형상이 존재하나
---------------------
admin-api 는 ``budget:config:user:{uid}`` 를 **Lua 로 병합**해 쓴다. 그 키에는 총액
예산 필드와 앱별 하위 한도 게이트(``app_clients``)가 함께 사는데, 쓰는 주체가 다르고
시점도 달라서 각자 GET-modify-SET 하면 서로의 필드를 지웠다 — CLI 로그인 한 번으로
그 사용자의 앱별 예산이 영구히 꺼지는 형태였다(admin-api core/budget_cache.py).

그런데 Redis 의 cjson 에는 **빈 배열 개념이 없다.** ``[]`` 를 round-trip 하면 ``{}`` 가
된다(실측: redis 7.4 에서 ``cjson.empty_array`` 가 nil). 둘 다 "활성 per-app 예산이
없다" 는 같은 뜻이다.

무엇이 걸려 있나
----------------
예전 게이트는 ``isinstance(user_app_clients, list)`` 로만 판정했다. ``{}`` 는 dict 라
그 검사에서 떨어지고, 그러면 per-app 분기 **전체**를 건너뛴다 — 그 안에는 per-app
config 콜드 캐시 재수화 안전망(``_hydrate_client_config_cache``)도 들어 있다.

지금은 결과가 우연히 같다(per-app 예산 0건이면 어차피 평가할 것이 없다). 그 우연에
기대지 않으려고 한 곳에서 정규화하고, 여기서 그 계약을 못 박는다.
"""

from __future__ import annotations

import pytest

from app.services.budget_service import _as_client_list


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # 정상 형상
        ([], []),
        (["codex"], ["codex"]),
        (["claude-code", "cowork", "codex"], ["claude-code", "cowork", "codex"]),
        # cjson 이 빈 배열을 round-trip 한 결과
        ({}, []),
        # 필드 부재
        (None, []),
    ],
)
def test_normalization(raw, expected):
    assert _as_client_list(raw) == expected


def test_the_two_empty_shapes_are_indistinguishable_after_normalization():
    """이게 이 함수의 존재 이유다 — 한 줄로 요약한 계약."""
    assert _as_client_list([]) == _as_client_list({})


@pytest.mark.parametrize("weird", [{"codex": 1}, {"a": "b"}, "codex", 0, 1, 3.14, True])
def test_unexpected_shapes_fall_back_to_empty_not_to_a_guess(weird):
    """예상 밖 형상을 **추측해서 통과시키면 안 된다.**

    특히 비어 있지 않은 dict: 키를 client 이름으로 착각해 리스트로 바꾸면, 존재하지 않는
    per-app 예산을 "있다" 고 판정해 없는 한도를 평가하게 된다. 빈 리스트로 떨어뜨리면
    per-app 평가를 건너뛰고 **부모 USER 총예산은 그대로 걸린다** — 안전한 방향이다.

    ⚠️ 문자열도 마찬가지다. ``"codex"`` 를 그대로 넘기면 ``client in "codex"`` 가
       부분 문자열 매칭이 되어 ``"code"`` 같은 값이 통과할 수 있다.
    """
    assert _as_client_list(weird) == []


def test_elements_are_stringified():
    """숫자가 섞여 와도 client 비교(문자열)와 형이 맞아야 한다."""
    assert _as_client_list(["codex", 1]) == ["codex", "1"]


def test_gate_uses_the_normalizer():
    """정규화 함수를 만들어 두고 게이트에서 쓰지 않으면 아무 효과가 없다.

    주석이 아니라 실행부에서 판정한다.
    """
    import ast
    from pathlib import Path

    src_file = (
        Path(__file__).resolve().parents[2] / "src" / "app" / "services" / "budget_service.py"
    )
    tree = ast.parse(src_file.read_text(encoding="utf-8"))

    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_as_client_list"
    ]
    # 정의 자체는 Call 이 아니므로, 호출이 1건 이상이어야 한다.
    assert calls, "_as_client_list 가 어디서도 호출되지 않는다 — 정규화가 죽어 있다"

    # 옛 게이팅 형태가 남아 있지 않아야 한다. 판정은 **AST 에서** 한다 —
    # `isinstance(user_app_clients, list)` 라는 정확한 호출 형상만 본다. 파일 전체를
    # 문자열로 검사하면 무관한 isinstance(다른 변수에 대한 것)에 걸린다.
    bad = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "isinstance"
        and n.args
        and isinstance(n.args[0], ast.Name)
        and n.args[0].id == "user_app_clients"
    ]
    assert not bad, (
        "게이트가 여전히 isinstance(user_app_clients, list) 로 판정한다 — "
        "cjson 의 빈 dict 를 거부해 per-app 분기 전체를 건너뛴다"
    )
