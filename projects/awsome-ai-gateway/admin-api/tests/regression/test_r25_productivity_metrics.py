# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: R25 productivity 지표 의미론 — 수용률·산출물당 비용.

- **N13** — code_acceptance_rate 는 이벤트 수가 아니라 **라인 수** 비율이다.
  100줄 생성/1줄 수용을 100% 로 치면 지표가 거짓이 된다. 라인 컬럼이 전부 0 인
  (구형 이벤트만 있는) 월만 이벤트 수로 폴백한다.

- **N8 / R2-N8** — "산출물당 비용" 의 분자는 분모와 **같은 코호트** 의 비용이어야
  한다. 분자가 전사 비용이면 비계측 사용자의 비용이 얹혀 지표가 부풀려진다.
  cost_per_generated_line ← productivity 이벤트 사용자 비용,
  cost_per_commit ← git 이벤트 사용자 비용 + user_id 귀속 커밋만 분모.
"""

from __future__ import annotations

import pytest
from types import SimpleNamespace

from app.routers.productivity import get_productivity_analytics


class _Row:
    """one() 용 — 속성 접근 결과 행."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Result:
    def __init__(self, row=None, scalar=0):
        self._row = row
        self._scalar = scalar

    def one(self):
        return self._row

    def scalar_one(self):
        return self._scalar


@pytest.fixture
def mock_session():
    """get_productivity_analytics 의 execute 호출 순서에 맞춘 캔 큐.

    호출 순서 (productivity.py):
      1 prod_stmt      → .one()  (lines/events)
      2 commit_stmt    → .one()  (total_commits, cohort_commits)
      3 pr_opened      → .scalar_one()
      4 pr_merged      → .scalar_one()
      5 active_devs    → .scalar_one()
      6 total_cost     → .scalar_one()
      7 prod_cohort    → .scalar_one()
      8 git_cohort     → .scalar_one()
    """

    class _QueueSession:
        def __init__(self, results):
            self._queue = list(results)

        async def execute(self, _stmt):
            assert self._queue, "예상보다 많은 execute 호출"
            return self._queue.pop(0)

    return _QueueSession


def _prod_row(lines_gen=0, lines_acc=0, acc_ev=0, gen_ev=0):
    return _Result(
        row=_Row(
            total_events=acc_ev + gen_ev,
            total_lines_generated=lines_gen,
            total_lines_accepted=lines_acc,
            accepted_count=acc_ev,
            generated_count=gen_ev,
        )
    )


def _commit_row(total=0, cohort=0):
    return _Result(row=_Row(total_commits=total, cohort_commits=cohort))


async def _call(session):
    return await get_productivity_analytics(
        request=None, period="2026-09", admin=None, session=session
    )


async def test_acceptance_rate_is_line_based_not_event_based(mock_session):
    """1건 생성 이벤트(1000줄) + 1건 수용 이벤트(400줄) — 이벤트 비율이면
    100% 지만 라인 비율은 40.0 여야 한다."""
    session = mock_session(
        [
            _prod_row(lines_gen=1000, lines_acc=400, acc_ev=1, gen_ev=1),
            _commit_row(),
            _Result(scalar=0),  # pr_opened
            _Result(scalar=0),  # pr_merged
            _Result(scalar=1),  # active_devs
            _Result(scalar=100.0),  # total_cost
            _Result(scalar=10.0),  # prod_cohort_cost
            _Result(scalar=0.0),  # git_cohort_cost
        ]
    )
    out = await _call(session)
    assert out["productivity"]["code_acceptance_rate_pct"] == pytest.approx(40.0)


async def test_cost_per_line_uses_productivity_cohort_cost(mock_session):
    """분자는 전사 비용이 아니라 productivity 코호트 비용이다."""
    session = mock_session(
        [
            _prod_row(lines_gen=100, lines_acc=50),
            _commit_row(),
            _Result(scalar=0),
            _Result(scalar=0),
            _Result(scalar=1),
            _Result(scalar=1000.0),  # total_cost — 쓰이면 안 됨
            _Result(scalar=10.0),  # prod_cohort_cost
            _Result(scalar=0.0),
        ]
    )
    out = await _call(session)
    assert out["roi"]["cost_per_generated_line"] == pytest.approx(0.1)


async def test_cost_per_commit_uses_git_cohort_and_attributed_commits(mock_session):
    """분모는 user-attributed 커밋(cohort_commits), 분자는 git 코호트 비용."""
    session = mock_session(
        [
            _prod_row(),
            _commit_row(total=10, cohort=8),
            _Result(scalar=0),
            _Result(scalar=0),
            _Result(scalar=3),
            _Result(scalar=0.0),
            _Result(scalar=0.0),
            _Result(scalar=20.0),  # git_cohort_cost
        ]
    )
    out = await _call(session)
    # 표시용 total_commits 는 전체(10)를 유지하고, ROI 분모만 8 로 좁힌다.
    assert out["productivity"]["total_commits"] == 10
    assert out["roi"]["cost_per_commit"] == pytest.approx(2.5)
