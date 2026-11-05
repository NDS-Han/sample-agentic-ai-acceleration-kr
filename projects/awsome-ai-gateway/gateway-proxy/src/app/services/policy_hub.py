"""The AI Governance Hub half of GET /v1/policy.

The gateway computes the MODELS part of a Claude Code policy itself (policy_service —
the same tables it enforces at inference time). Everything else a team policy carries —
plugin marketplaces, enabled plugins, managed MCP servers, the lock keys — is owned by
the hub, which the gateway asks here with the identity the VK already carries
(user_id, team_id: no DB join). The hub's answer goes UNDER the models fragment and any
model key in it is dropped, so there is exactly one source for availableModels /
enforceAvailableModels / model and the server-enforced list can never disagree with
Claude Code's /model list.

Failure policy: unset URL, timeout, non-200 or an unusable body all degrade to {} — the
models-only answer — with a warning. Never a 503 here: the helper would then keep its
whole cached policy instead of degrading to the models slice it can still get.

Contract (hub side): POST {POLICY_HUB_URL}/v1/resolve, Authorization: Bearer <token>,
body {"user_id", "team_id", "client": "claude-code"} -> {"version", "managedSettings"}.
"""
from __future__ import annotations

import httpx
import structlog

from app.config import get_settings

logger = structlog.get_logger(__name__)

CLIENT = "claude-code"
#: Keys only the gateway may set (see models_fragment). Dropped from whatever the hub sends.
MODEL_KEYS = frozenset({"availableModels", "enforceAvailableModels", "model"})

_shared_client: httpx.AsyncClient | None = None


def _client() -> httpx.AsyncClient:
    """프로세스에 하나뿐인 지연 생성 클라이언트(클러스터 안 허브와 keep-alive 유지).

    One lazily created client for the process (keep-alive to the in-cluster hub).
    """
    global _shared_client
    if _shared_client is None:
        _shared_client = httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=5),
        )
    return _shared_client


async def fetch_fragment(
    client: httpx.AsyncClient, *, url: str, token: str, timeout: float, user_id: str, team_id: str,
) -> dict:
    """허브가 주는 이 사용자의 비(非)모델 조각. 어떤 실패든 {} 을 돌려주고 경고 로그만 남긴다.

    This user's non-model fragment from the hub, or {} for any failure (logged).
    """
    if not url:
        return {}
    try:
        resp = await client.post(
            url.rstrip("/") + "/v1/resolve",
            json={"user_id": user_id, "team_id": team_id, "client": CLIENT},
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            timeout=timeout,
        )
    except httpx.HTTPError as exc:
        logger.warning(
            "policy_hub_unreachable", error=f"{type(exc).__name__}: {exc}", team_id=team_id,
        )
        return {}
    if resp.status_code != 200:
        logger.warning("policy_hub_error", status=resp.status_code, team_id=team_id)
        return {}
    try:
        body = resp.json()
    except ValueError:
        logger.warning("policy_hub_bad_body", reason="not json", team_id=team_id)
        return {}
    fragment = body.get("managedSettings") if isinstance(body, dict) else None
    if not isinstance(fragment, dict) or any(
        not isinstance(k, str) or k.startswith("_") for k in fragment
    ):
        logger.warning(
            "policy_hub_bad_body", reason="managedSettings not a settings object", team_id=team_id,
        )
        return {}
    return {k: v for k, v in fragment.items() if k not in MODEL_KEYS}


async def hub_fragment(user_id: str, team_id: str) -> dict:
    """배포 설정값과 공유 클라이언트로 fetch_fragment 를 부르는 라우터 진입점.

    fetch_fragment with the deployment's settings and the shared client (router entry).
    """
    s = get_settings()
    if not s.policy_hub_url:  # no hub in this deployment: no client, no call
        return {}
    return await fetch_fragment(
        _client(), url=s.policy_hub_url, token=s.policy_hub_token, timeout=s.policy_hub_timeout,
        user_id=user_id, team_id=team_id,
    )
