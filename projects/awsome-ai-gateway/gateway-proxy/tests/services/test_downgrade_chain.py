# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

from app.services.downgrade_loader import DowngradeRule, apply_chain


def make(rules):
    return [DowngradeRule(*r) for r in rules]


def test_no_rules_returns_original():
    assert apply_chain("opus", [], current_pct=99)[0] == "opus"


def test_threshold_not_met_returns_original():
    rules = make([("opus", "sonnet", 80)])
    assert apply_chain("opus", rules, current_pct=70)[0] == "opus"


def test_single_match_applied():
    rules = make([("opus", "sonnet", 80)])
    assert apply_chain("opus", rules, current_pct=85)[0] == "sonnet"


def test_chain_applied_iteratively():
    rules = make(
        [
            ("opus", "sonnet", 70),
            ("sonnet", "haiku", 90),
        ]
    )
    result, hops = apply_chain("opus", rules, current_pct=95)
    assert (result, hops) == ("haiku", 2)


def test_chain_stops_when_no_further_rule():
    rules = make(
        [
            ("opus", "sonnet", 70),
            ("sonnet", "haiku", 99),  # current_pct=80 < 99, 멈춤
        ]
    )
    assert apply_chain("opus", rules, current_pct=80)[0] == "sonnet"


def test_cycle_prevented_by_visited_set():
    rules = make(
        [
            ("opus", "sonnet", 50),
            ("sonnet", "opus", 50),
        ]
    )
    # opus -> sonnet (visited={opus,sonnet}) -> next would be opus, blocked
    assert apply_chain("opus", rules, current_pct=99)[0] == "sonnet"


def test_max_depth_safety():
    # 6단 체인 (max_depth=5)이면 5번까지만
    rules = make(
        [
            ("a", "b", 1),
            ("b", "c", 1),
            ("c", "d", 1),
            ("d", "e", 1),
            ("e", "f", 1),
            ("f", "g", 1),
        ]
    )
    assert apply_chain("a", rules, current_pct=99, max_depth=5)[0] == "f"


def test_no_match_for_unknown_alias():
    rules = make([("opus", "sonnet", 50)])
    assert apply_chain("haiku", rules, current_pct=99)[0] == "haiku"


def test_multiple_rules_picks_highest_satisfied_threshold():
    """같은 from_alias 에 여러 임계값 규칙이 있으면 사용률을 만족하는 것 중
    **가장 높은** 임계값의 규칙이 선택되어야 한다 (단계적 하향 의도)."""
    rules = make(
        [
            ("opus", "sonnet", 80),
            ("opus", "haiku", 100),
        ]
    )
    # 100% 도달 — 80%→sonnet 이 아니라 100%→haiku 규칙이 적용되어야 한다.
    assert apply_chain("opus", rules, current_pct=100)[0] == "haiku"
    # 85% — 100% 규칙은 미충족 → 80% 규칙.
    assert apply_chain("opus", rules, current_pct=85)[0] == "sonnet"
