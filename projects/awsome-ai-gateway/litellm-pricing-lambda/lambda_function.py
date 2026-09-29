"""LiteLLM Model Catalog fetch proxy — llm-gateway-<env>-litellm-pricing.

admin-api 가 직접 api.litellm.ai 를 호출하는 대신 이 Lambda 를 invoke 한다
(외부 카탈로그 호출을 데이터 플레인 밖으로 격리). 반환은 **원본 모델 목록** —
per-1k 정규화·region-aware lookup·diff/apply 는 admin-api 의
LiteLLMPricingSyncService 가 그대로 수행한다.

환경변수:
  LITELLM_BASE_URL        기본 https://api.litellm.ai
  LITELLM_PROVIDER_FILTER 기본 bedrock_converse (빈 문자열이면 전체)
  LITELLM_PAGE_SIZE       기본 100

응답(JSON):
  성공: {"data": [<model_catalog row>, ...], "pages": <n>}
  실패: {"errors": ["<message>"]}        — FunctionError 없이 200 으로 반환해
                                         admin-api 가 fail-soft 로 처리
반환 payload 는 Lambda 동기 invoke 제한(6MB) 안에 들어와야 하므로 원본 row 를
그대로 넘긴다(필드 필터링은 하지 않음 — catalog 스키마가 바뀌어도 깨지지 않게).
"""

import json
import os
import urllib.parse
import urllib.request

BASE_URL = os.environ.get("LITELLM_BASE_URL", "https://api.litellm.ai").rstrip("/")
PROVIDER_FILTER = os.environ.get("LITELLM_PROVIDER_FILTER", "bedrock_converse")
PAGE_SIZE = int(os.environ.get("LITELLM_PAGE_SIZE", "100"))
MAX_PAGES = 50  # 방어적 상한 — has_more 가 끊기지 않는 비정상 응답 대비
REQUEST_TIMEOUT = 20


def _fetch_page(page: int) -> dict:
    params = {"page_size": PAGE_SIZE, "page": page}
    if PROVIDER_FILTER:
        params["provider"] = PROVIDER_FILTER
    url = f"{BASE_URL}/model_catalog?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        return json.loads(resp.read())


def lambda_handler(event, context):
    models: list[dict] = []
    try:
        for page in range(1, MAX_PAGES + 1):
            body = _fetch_page(page)
            models.extend(body.get("data", []))
            if not body.get("has_more"):
                return {"data": models, "pages": page}
        return {"errors": [f"page limit exceeded ({MAX_PAGES})"], "data": models}
    except Exception as e:  # noqa: BLE001 — 외부 API 실패는 결과로 보고
        return {"errors": [f"LiteLLM catalog fetch failed: {e}"]}
