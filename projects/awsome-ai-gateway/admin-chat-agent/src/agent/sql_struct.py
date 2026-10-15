# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""sql_struct — SQL 의 결정적 구조 사실 추출 (DEVLOG §58, L2).

ask_validator 가 'SQL 텍스트 + 20행 샘플'만 보고 의도부합을 LLM 으로 *추정*하던
PASS 편향(감사 결함: validator 가 틀린 집계를 PASS)을 억제하기 위해, sqlglot AST
에서 검증가능한 구조 사실(참조 테이블·JOIN 키·집계 함수와 대상 컬럼·GROUP BY 키·
WHERE 술어·COUNT(DISTINCT) 유무·timezone 앵커 유무)을 결정적으로 추출한다.

validator 는 이 fact sheet 를 받아 '문장이 그럴듯한가'가 아니라 '구조가 의도와
맞는가'를 항목별 rubric 으로 채점한다(G-Eval/structured-judge 의 SQL 특화).

sqlglot 외 의존성 없음. 추출 실패는 빈 dict 로 graceful(검증을 막지 않음).
"""

from __future__ import annotations

from typing import Any

try:
    import sqlglot
    import sqlglot.expressions as exp
except ImportError:  # pragma: no cover
    sqlglot = None
    exp = None


def _scope_sources(sel: Any) -> dict[str, str]:
    """한 Select 스코프의 FROM/JOIN 에 선언된 alias-or-name(소문자) ->
    'schema.table'. 전 스코프를 한 map 으로 합치면 서브쿼리·CTE 의 동명
    별칭이 외부 테이블로 오배정된다 — 스코프별로 따로 잡는다."""
    src: dict[str, str] = {}
    containers: list[Any] = []
    f = sel.args.get("from_")
    if f is not None:
        containers.append(f.this)
    for j in sel.args.get("joins") or []:
        containers.append(j.this)
    for c in containers:
        if not isinstance(c, exp.Table):
            continue  # 서브쿼리/CTE 참조 — 물리 테이블이 아니다
        sch = (c.text("db") or "").lower()
        name = (c.name or "").lower()
        src[(c.alias or name).lower()] = f"{sch}.{name}" if sch else name
    return src


def _col_fq(col: Any) -> str:
    """컬럼의 테이블 한정자를 **가장 안쪽 스코프부터** 바깥으로 해석한다.

    서브쿼리가 외부와 같은 별칭을 다르메 쓰면(``FROM t`` 안의
    ``(SELECT … FROM x t)``) 글로벌 map 은 마지막에 본 쪽으로 전부
    오배정한다 — 상관 서브쿼리의 외부 참조를 위해 안쪽에서 못 찾으면
    바깥 스코프로 계속 올라간다."""
    tbl = (col.table or "").lower()
    name = (col.name or "").lower()
    if not tbl:
        return name
    node = col.parent
    while node is not None:
        if isinstance(node, exp.Select):
            src = _scope_sources(node)
            if tbl in src:
                return f"{src[tbl]}.{name}"
        node = node.parent
    return f"{tbl}.{name}"


def extract_facts(sql: str) -> dict[str, Any]:
    """SQL → 구조 사실 dict. 실패 시 {'_parse_ok': False}.

    반환 키:
      tables: ["usage.usage_logs", ...]
      joins: [{"table": ..., "on": "...", "via_pk": bool}]
      aggregates: [{"func": "SUM", "column": "usage.usage_logs.cost_usd", "distinct": bool}]
      group_by: ["auth.users.email", ...]
      where_columns: ["usage.usage_logs.status", ...]
      filters: ["status = 'SUCCESS'", ...]   # 술어 텍스트
      has_count_distinct: bool
      timestamptz_anchored: bool | None       # 시간필터가 AT TIME ZONE 으로 감싸졌나
      has_time_filter: bool
    """
    if sqlglot is None:
        return {"_parse_ok": False}
    try:
        ast = sqlglot.parse_one(sql, dialect="postgres")
    except Exception:  # noqa: BLE001
        return {"_parse_ok": False}
    if ast is None:
        return {"_parse_ok": False}

    facts: dict[str, Any] = {"_parse_ok": True}

    # CTE 이름 — `FROM t` 의 t 는 Table 노드로 파싱되지만 실제 테이블이 아니라
    # CTE 참조다. 비한정 이름만 제외한다(schema 한정된 동명은 진짜 테이블).
    cte_names = {
        (c.alias or "").lower() for c in ast.find_all(exp.CTE) if c.alias
    }

    # tables
    tables = []
    for t in ast.find_all(exp.Table):
        sch = (t.text("db") or "").lower()
        name = (t.name or "").lower()
        if not sch and name in cte_names:
            continue
        fq = f"{sch}.{name}" if sch else name
        if fq not in tables:
            tables.append(fq)
    facts["tables"] = tables

    # joins
    joins = []
    for j in ast.find_all(exp.Join):
        on = j.args.get("on")
        jt = j.find(exp.Table)
        joins.append(
            {
                "table": (f"{(jt.text('db') or '').lower()}.{(jt.name or '').lower()}".strip(".") if jt else "?"),
                "on": on.sql(dialect="postgres") if on else None,
            }
        )
    facts["joins"] = joins

    # aggregates
    aggs = []
    agg_types = {
        exp.Sum: "SUM",
        exp.Avg: "AVG",
        exp.Count: "COUNT",
        exp.Min: "MIN",
        exp.Max: "MAX",
    }
    has_count_distinct = False
    for agg in ast.find_all(exp.AggFunc):
        fname = agg_types.get(type(agg), type(agg).__name__.upper())
        distinct = bool(agg.args.get("distinct")) or isinstance(agg.this, exp.Distinct)
        cols = [_col_fq(c) for c in agg.find_all(exp.Column)]
        if fname == "COUNT" and distinct:
            has_count_distinct = True
        aggs.append(
            {"func": fname, "columns": cols, "distinct": distinct}
        )
    facts["aggregates"] = aggs
    facts["has_count_distinct"] = has_count_distinct

    # group by — CTE/서브쿼리까지 **모든** Select 스코프를 본다(최외곽만 보면
    # 집계가 내부에 숨은 쿼리를 "GROUP BY 없음"으로 오보고한다).
    gb: list[str] = []
    selects = list(ast.find_all(exp.Select))
    if isinstance(ast, exp.Select) and not any(s is ast for s in selects):
        selects.insert(0, ast)
    for sel in selects:
        group = sel.args.get("group")
        if group is None:
            continue
        for e in group.expressions:
            cols = list(e.find_all(exp.Column))
            if cols:
                for c in cols:
                    fq = _col_fq(c)
                    if fq not in gb:
                        gb.append(fq)
            else:
                # 위치 기반 GROUP BY 1 → 해당 select 표현식으로 해석한다.
                # 컬럼 해석이 안 되는 표현식·리터럴은 텍스트로라도 사실을
                # 남긴다(빈 보고보다 정확하다).
                txt = None
                if e.is_int:
                    idx = int(e.this) - 1
                    projs = sel.expressions
                    if 0 <= idx < len(projs):
                        # 컬럼을 품은 투영이면 FQ 로 푼다 — 't.user_id' 보다
                        # 'usage.usage_logs.user_id' 가 의도 판정에 정확하다.
                        pcols = list(projs[idx].find_all(exp.Column))
                        if pcols:
                            txt = ", ".join(dict.fromkeys(_col_fq(c) for c in pcols))
                        else:
                            txt = projs[idx].sql(dialect="postgres")
                if txt is None:
                    try:
                        txt = e.sql(dialect="postgres")
                    except Exception:  # noqa: BLE001
                        txt = "?"
                if txt not in gb:
                    gb.append(txt)
    facts["group_by"] = gb

    # where columns + filter texts — 모든 스코프의 WHERE 를 모은다.
    where_cols: list[str] = []
    filters: list[str] = []
    for where in ast.find_all(exp.Where):
        for c in where.find_all(exp.Column):
            fq = _col_fq(c)
            if fq not in where_cols:
                where_cols.append(fq)
        # 술어 텍스트(EQ/IN 등) — 짧게. 중첩 술어는 부모가 자식을 다시
        # 품지 않으므로 EQ 안의 EQ 같은 이중 보고는 없지만, 동일 술어가
        # 두 스코프에 있으면 중복된다 — 중복은 한 번만 남긴다.
        for pred in where.find_all((exp.EQ, exp.In, exp.GTE, exp.LTE, exp.GT, exp.LT)):
            try:
                txt = pred.sql(dialect="postgres")
            except Exception:  # noqa: BLE001
                continue
            if txt not in filters:
                filters.append(txt)
    facts["where_columns"] = where_cols
    facts["filters"] = filters[:8]

    # timezone anchoring on time filters
    has_atz = bool(list(ast.find_all(exp.AtTimeZone)))
    has_time_filter = bool(
        list(ast.find_all(exp.Interval))
        or list(ast.find_all(exp.DateTrunc))
        or any((fn.name or "").lower() == "now" for fn in ast.find_all(exp.Anonymous))
        or list(ast.find_all(exp.CurrentTimestamp))
    )
    facts["has_time_filter"] = has_time_filter
    facts["timestamptz_anchored"] = (has_atz if has_time_filter else None)

    return facts


def facts_to_prompt(facts: dict[str, Any]) -> str:
    """fact sheet → validator 프롬프트에 넣을 사람 읽기용 구조 요약."""
    if not facts.get("_parse_ok"):
        return "(SQL 구조 파싱 실패 — 텍스트만으로 판정)"
    lines = []
    lines.append(f"- 참조 테이블: {', '.join(facts.get('tables') or []) or '(없음)'}")
    joins = facts.get("joins") or []
    if joins:
        lines.append("- JOIN:")
        for j in joins:
            lines.append(f"    · {j.get('table')} ON {j.get('on')}")
    aggs = facts.get("aggregates") or []
    if aggs:
        lines.append("- 집계:")
        for a in aggs:
            d = " DISTINCT" if a.get("distinct") else ""
            lines.append(f"    · {a.get('func')}{d}({', '.join(a.get('columns') or [])})")
    lines.append(f"- GROUP BY: {', '.join(facts.get('group_by') or []) or '(없음)'}")
    lines.append(f"- WHERE 컬럼: {', '.join(facts.get('where_columns') or []) or '(없음)'}")
    if facts.get("filters"):
        lines.append(f"- 필터 술어: {' ; '.join(facts['filters'])}")
    lines.append(f"- COUNT(DISTINCT) 사용: {'예' if facts.get('has_count_distinct') else '아니오'}")
    if facts.get("has_time_filter"):
        anchored = facts.get("timestamptz_anchored")
        lines.append(
            f"- 시간 필터 KST 앵커(AT TIME ZONE 'Asia/Seoul'): "
            f"{'있음' if anchored else '없음 ⚠️ (KST 자정과 9시간 어긋날 수 있음)'}"
        )
    return "\n".join(lines)
