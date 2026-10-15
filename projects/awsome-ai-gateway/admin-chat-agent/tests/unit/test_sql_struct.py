# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""sql_struct 회귀 — R18 S4: CTE/서브쿼리 스코프와 위치 기반 GROUP BY 커버리지."""
import pytest

from agent.sql_struct import extract_facts


@pytest.mark.unit
def test_cte_name_not_listed_as_table():
    facts = extract_facts(
        """
        WITH t AS (SELECT user_id, sum(cost_usd) c FROM usage.usage_logs
                   WHERE status='SUCCESS' GROUP BY user_id)
        SELECT t.user_id, u.email FROM t JOIN auth.users u ON u.id = t.user_id
        """
    )
    assert facts["_parse_ok"] is True
    assert "t" not in facts["tables"]
    assert "usage.usage_logs" in facts["tables"]
    assert "auth.users" in facts["tables"]


@pytest.mark.unit
def test_group_by_where_inside_cte_are_reported():
    # 최외곽 Select 만 보던 결함 — CTE 내부 집계/필터가 "(없음)" 으로 오보고됐다.
    facts = extract_facts(
        """
        WITH t AS (SELECT user_id, sum(cost_usd) c FROM usage.usage_logs
                   WHERE status='SUCCESS' GROUP BY user_id)
        SELECT t.user_id FROM t
        """
    )
    assert facts["group_by"]
    assert "status" in facts["where_columns"] or any(
        "status" in w for w in facts["where_columns"]
    )
    assert any("SUCCESS" in f for f in facts["filters"])


@pytest.mark.unit
def test_positional_group_by_resolves_to_projection():
    facts = extract_facts(
        "SELECT user_id, count(*) FROM usage.usage_logs GROUP BY 1"
    )
    assert facts["group_by"] == ["user_id"]


@pytest.mark.unit
def test_schema_qualified_same_name_as_cte_is_real_table():
    # 비한정 이름만 CTE 로 숨는다 — schema 한정된 동명은 진짜 테이블.
    facts = extract_facts(
        """
        WITH t AS (SELECT 1 AS x)
        SELECT * FROM t JOIN usage.t ON true
        """
    )
    assert "t" not in facts["tables"]
    assert "usage.t" in facts["tables"]


@pytest.mark.unit
def test_subquery_where_is_reported():
    facts = extract_facts(
        """
        SELECT user_id FROM auth.users
        WHERE id IN (SELECT user_id FROM usage.usage_logs
                     WHERE model_alias = 'claude-haiku-5-5')
        """
    )
    assert any("model_alias" in w for w in facts["where_columns"])


@pytest.mark.unit
def test_inner_scope_alias_shadows_outer_table():
    # 서브쿼리가 외부와 같은 별칭 t 를 다른 테이블에 쓰면, 글로벌 map 은
    # 마지막에 본 쪽으로 전부 오배정했다 — 내부 t 는 usage.other,
    # 외부 t 는 usage.usage_logs 여야 한다.
    facts = extract_facts(
        """
        SELECT t.user_id, (SELECT sum(t.cost_usd) FROM usage.other t) AS s
          FROM usage.usage_logs t
         WHERE t.status = 'SUCCESS'
        """
    )
    assert "usage.usage_logs.status" in facts["where_columns"]
    agg_cols = [c for a in facts["aggregates"] for c in a["columns"]]
    assert "usage.other.cost_usd" in agg_cols


@pytest.mark.unit
def test_correlated_subquery_resolves_outer_alias():
    # 안쪽 스코프에 없는 별칭은 바깥으로 올라가 해석한다(상관 서브쿼리).
    facts = extract_facts(
        """
        SELECT u.email FROM auth.users u
         WHERE EXISTS (SELECT 1 FROM usage.logs l WHERE l.user_id = u.user_id)
        """
    )
    assert "auth.users.user_id" in facts["where_columns"]
    assert "usage.logs.user_id" in facts["where_columns"]


@pytest.mark.unit
def test_positional_group_by_resolves_to_fq_column():
    # GROUP BY 1 → 투영 컬럼의 FQ — 't.user_id' 원문보다 정확하다.
    facts = extract_facts(
        "SELECT t.user_id, sum(t.cost_usd) FROM usage.usage_logs t GROUP BY 1"
    )
    assert facts["group_by"] == ["usage.usage_logs.user_id"]
