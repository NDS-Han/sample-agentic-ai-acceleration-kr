"""LiteLLM Model Catalog fetch proxy — llm-gateway-<env>-litellm-pricing.

admin-api 가 직접 api.litellm.ai 를 호출하는 대신 이 Lambda 를 invoke 한다
(외부 카탈로그 호출을 데이터 플레인 밖으로 격리). 반환은 **원본 모델 목록** —
per-1k 정규화·region-aware lookup·diff/apply 는 admin-api 의
LiteLLMPricingSyncService 가 그대로 수행한다.

환경변수:
  LITELLM_BASE_URL        기본 https://api.litellm.ai
  LITELLM_PROVIDER_FILTER 기본 bedrock_converse (빈 문자열이면 전체)
                          cost-map 모드에서는 litellm_provider 필드에 적용 — 쉼표로
                          복수 지정 가능. cost map 의 global/CRIS 모델은
                          "bedrock_converse" 값을 쓰므로 기본값 그대로면 충분
  LITELLM_PAGE_SIZE       기본 100
  LITELLM_COST_MAP_URL    설정 시 cost-map 모드 — 이 URL 한 곳에서
                          {"<model_id>": {entry}} 형태의 JSON 을 받아
                          [{"id": model_id, **entry}] 로 변환한다.
                          api.litellm.ai 의 model_catalog API 가 없을 때의 폴백 —
                          canonical 소스는 GitHub 의
                          model_prices_and_context_window.json (약 3MB, 동기
                          invoke 제한 6MB 안).

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
COST_MAP_URL = os.environ.get("LITELLM_COST_MAP_URL", "").strip()
try:
    PAGE_SIZE = int(os.environ.get("LITELLM_PAGE_SIZE", "100"))
    if not (1 <= PAGE_SIZE <= 1000):
        raise ValueError
except ValueError:
    # 잘못된 env 하나로 Lambda cold start 가 죽지 않게 기본값으로 폴백.
    PAGE_SIZE = 100
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


def _fetch_cost_map() -> list[dict]:
    """cost-map 모드 — 단일 JSON 파일을 받아 model_catalog rows 형태로 변환.

    파일은 {"<model_id>": {entry}} 형태이고 각 entry 에 litellm_provider 가
    있다. PROVIDER_FILTER 를 쉼표 목록으로 해석해 그 필드에 적용한다.
    cost map 의 bedrock 계열 값은 세 갈래다 — "bedrock_converse"(CRIS/global
    모델, 기본값 그대로 사용), "bedrock"(구형 foundation-model 직접호출 ID),
    "bedrock_mantle"("bedrock_mantle/..." 접두 키).
    """
    providers = {p.strip() for p in PROVIDER_FILTER.split(",") if p.strip()}
    req = urllib.request.Request(COST_MAP_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, dict):
        raise ValueError("cost map top-level is not an object")
    rows = []
    for model_id, entry in data.items():
        if not isinstance(entry, dict):
            continue
        if providers and entry.get("litellm_provider") not in providers:
            continue
        row = dict(entry)
        row.setdefault("id", model_id)
        rows.append(row)
    return rows


def lambda_handler(event, context):
    models: list[dict] = []
    try:
        if COST_MAP_URL:
            return {"data": _fetch_cost_map(), "pages": 1}
        for page in range(1, MAX_PAGES + 1):
            body = _fetch_page(page)
            models.extend(body.get("data", []))
            if not body.get("has_more"):
                return {"data": models, "pages": page}
        return {"errors": [f"page limit exceeded ({MAX_PAGES})"], "data": models}
    except Exception as e:  # noqa: BLE001 — 외부 API 실패는 결과로 보고
        return {"errors": [f"LiteLLM catalog fetch failed: {e}"]}
