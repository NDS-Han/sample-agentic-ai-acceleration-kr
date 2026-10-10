# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""BudgetRepository.get_usage — 총합 행(client IS NULL)만 읽는지.

cost-recorder-worker 는 Claude Code·Cowork·Codex 요청마다 USER 사용액을 두 행에 쓴다
(총합 client=NULL + 앱별 client='claude-code' 등). get_usage 가 client 조건 없이
scalar_one_or_none() 을 부르면 그 사용자에게서 MultipleResultsFound 가 나고,
팀 배분 화면(GET /budgets/team/{id}/allocation)이 500 이 된다.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy.dialects import postgresql

from app.models.budget import BudgetScope
from app.repositories.budget_repository import BudgetRepository


async def test_get_usage_reads_only_the_total_row():
    result = MagicMock()
    result.scalar_one_or_none = MagicMock(return_value=None)
    session = AsyncMock()
    session.execute = AsyncMock(return_value=result)

    await BudgetRepository(session).get_usage(BudgetScope.USER, uuid.uuid4(), "2026-10")

    stmt = session.execute.await_args.args[0]
    sql = str(stmt.compile(dialect=postgresql.dialect()))
    assert "budget_usages.client IS NULL" in sql, sql
