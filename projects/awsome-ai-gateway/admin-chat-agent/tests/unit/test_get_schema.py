# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""get_schema Lambda 회귀 — whitelist 메타데이터 해석.

정적 whitelist 를 읽어 테이블 메타만 반환하는 읽기 전용 경로. 커버 계약:
  - 인자 없음 → 전체 allowed_tables 목록 + 금지 목록
  - table_name → 대소문자 무관, 'schema.table'·'table' 두 형태 모두 허용
  - 미등록 테이블 → columns 노출 없이 에러 + 목록 안내
  - whitelist yaml 의 각 항목이 schema/table 키를 실제로 갖는지(형태 계약)
"""

import importlib.util
import os

import pytest

_LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "lambdas", "get_schema")
)

os.environ.setdefault(
    "SCHEMA_WHITELIST_PATH",
    os.path.join(os.path.dirname(__file__), "..", "..", "config", "schema_whitelist.yaml"),
)

# lambdas/*/lambda_function.py 가 전부 같은 모듈명이라 `import lambda_function`
# 으로는 query_db 테스트와 sys.modules 가 충돌한다 — 고유 이름으로 로드한다.
_spec = importlib.util.spec_from_file_location(
    "get_schema_lambda_function", os.path.join(_LAMBDA_DIR, "lambda_function.py")
)
L = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(L)


def test_no_args_lists_all_whitelisted_tables():
    res = L.lambda_handler({}, None)
    assert "tables" in res
    wl = L.get_whitelist()
    assert len(res["tables"]) == len(wl["allowed_tables"])
    for t in res["tables"]:
        assert set(t) == {"schema", "table", "description"}
    # 금지 목록도 함께 내려가 에이전트가 경계를 안다
    assert "forbidden_columns" in res
    assert "forbidden_operations" in res
    # 목록 응답엔 컬럼 상세를 싣지 않는다(노출 최소화)
    assert all("columns" not in t for t in res["tables"])


@pytest.mark.parametrize(
    "name",
    [
        "usage.usage_logs",   # schema-qualified
        "usage_logs",         # bare
        "USAGE.USAGE_LOGS",   # case-insensitive
        "Usage_Logs",
    ],
)
def test_table_lookup_case_and_qualification(name):
    res = L.lambda_handler({"table_name": name}, None)
    assert res.get("schema") == "usage"
    assert res.get("table") == "usage_logs"
    assert isinstance(res.get("columns"), list) and res["columns"]


def test_unknown_table_rejected_without_columns():
    res = L.lambda_handler({"table_name": "auth.secrets"}, None)
    assert "error" in res
    assert "columns" not in res
    assert "whitelist" in res["error"].lower()


def test_schema_mismatch_rejected():
    # 테이블은 있으나 다른 스키마 한정이면 거부
    res = L.lambda_handler({"table_name": "public.usage_logs"}, None)
    assert "error" in res


def test_whitelist_entries_have_required_keys():
    # 항목 형태 계약 — t["schema"]/t["table"] 직접 인덱싱이라 누락 시
    # KeyError 로 Lambda 가 죽는다(회귀 가드)
    for t in L.get_whitelist()["allowed_tables"]:
        assert t["schema"] and t["table"]
        assert isinstance(t.get("columns", []), list)


def test_whitelist_cache_reused():
    first = L.get_whitelist()
    second = L.get_whitelist()
    assert first is second
