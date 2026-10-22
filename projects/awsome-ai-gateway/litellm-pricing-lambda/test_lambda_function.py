"""litellm-pricing-lambda 단위 테스트 — urllib 를 mock 해 페이지네이션/실패를 검증.

실행: python3 -m pytest litellm-pricing-lambda/test_lambda_function.py
       (stdlib + pytest 만 필요 — venv 불요)
"""
import importlib.util
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

# lambda_function.py 는 패키지가 아니라 단일 파일 — 직접 로드.
_spec = importlib.util.spec_from_file_location(
    "lambda_function", Path(__file__).parent / "lambda_function.py"
)
lf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lf)


class _FakeResp:
    def __init__(self, body: dict):
        self._body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _catalog_responder(pages: list[dict]):
    calls = []
    def fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _FakeResp(pages[len(calls) - 1])
    return calls, fake_urlopen


def test_paginates_until_has_more_false():
    calls, fake = _catalog_responder([
        {"data": [{"id": "a"}], "has_more": True},
        {"data": [{"id": "b"}], "has_more": False},
    ])
    with patch.object(lf.urllib.request, "urlopen", fake):
        out = lf.lambda_handler({}, None)
    assert len(calls) == 2
    assert {m["id"] for m in out["data"]} == {"a", "b"}
    assert out["pages"] == 2


def test_provider_filter_in_query():
    calls, fake = _catalog_responder([{"data": [], "has_more": False}])
    with patch.object(lf.urllib.request, "urlopen", fake):
        lf.lambda_handler({}, None)
    assert "provider=bedrock_converse" in calls[0]


def test_upstream_error_is_fail_soft():
    def boom(req, timeout=None):
        raise TimeoutError("upstream timeout")
    with patch.object(lf.urllib.request, "urlopen", boom):
        out = lf.lambda_handler({}, None)
    assert out["errors"] and "timeout" in out["errors"][0]
    assert "data" not in out


def test_page_limit_guard():
    calls, fake = _catalog_responder([{"data": [{"id": "x"}], "has_more": True}] * 100)
    with patch.object(lf.urllib.request, "urlopen", fake):
        out = lf.lambda_handler({}, None)
    assert len(calls) == lf.MAX_PAGES
    assert "page limit" in out["errors"][0]


def test_cost_map_mode(monkeypatch):
    """LITELLM_COST_MAP_URL 이 설정되면 단일 JSON 을 catalog rows 로 변환하고
    litellm_provider 필드에 PROVIDER_FILTER(쉼표 목록)를 적용한다."""
    monkeypatch.setattr(lf, "COST_MAP_URL", "https://example.com/cost.json")
    monkeypatch.setattr(lf, "PROVIDER_FILTER", "bedrock,bedrock_converse")
    cost_map = {
        "global.openai.gpt-6.1-sol": {
            "litellm_provider": "bedrock",
            "input_cost_per_token": 2e-06,
            "output_cost_per_token": 1e-05,
        },
        "claude-sonnet-4-6": {
            "litellm_provider": "bedrock_converse",
            "input_cost_per_token": 3e-06,
        },
        "gpt-4o": {"litellm_provider": "openai"},  # 필터 밖 — 제외돼야 함
        "sample_spec": {"litellm_provider": "one of docs"},  # 스펙 행도 제외
    }
    calls, fake = _catalog_responder([cost_map])
    with patch.object(lf.urllib.request, "urlopen", fake):
        out = lf.lambda_handler({}, None)
    assert len(calls) == 1 and calls[0] == "https://example.com/cost.json"
    ids = {m["id"] for m in out["data"]}
    assert ids == {"global.openai.gpt-6.1-sol", "claude-sonnet-4-6"}
    row = next(m for m in out["data"] if m["id"] == "global.openai.gpt-6.1-sol")
    assert row["input_cost_per_token"] == 2e-06
    assert out["pages"] == 1


def test_cost_map_mode_fail_soft(monkeypatch):
    monkeypatch.setattr(lf, "COST_MAP_URL", "https://example.com/cost.json")

    def boom(req, timeout=None):
        raise TimeoutError("down")
    with patch.object(lf.urllib.request, "urlopen", boom):
        out = lf.lambda_handler({}, None)
    assert out["errors"] and "down" in out["errors"][0]


if __name__ == "__main__":
    import pytest  # noqa: E402

    sys.exit(pytest.main([__file__, "-q"]))
