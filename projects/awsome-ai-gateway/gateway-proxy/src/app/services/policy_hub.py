"""The AI Governance Hub half of GET /v1/policy.

The gateway computes the MODELS part of a Claude Code policy itself (policy_service —
the same tables it enforces at inference time). Everything else a team policy carries —
plugin marketplaces, enabled plugins, managed MCP servers, the lock keys — is owned by
the hub, which the gateway asks here with the identity the VK already carries
(user_id, team_id: no DB join). The hub's answer goes UNDER the models fragment and any
model key in it is dropped, so there is exactly one source for availableModels /
enforceAvailableModels / model and the server-enforced list can never disagree with
Claude Code's /model list.

Failure policy: an unset URL means "no hub in this deployment" and yields {} (models only).
Every other failure — timeout, connection error, non-200, unusable body — raises
HubUnavailable, which the router turns into a 503. The helper then keeps its last good
policy whole. (A models-only 200 would be worse: the helper would cache that reduced answer
and the team's plugins, MCP servers and locks would vanish from every PC until the hub is
back. Models are enforced server-side regardless, so a stale model list costs nothing.)

Allow-list: only HUB_KEYS may come from the hub. A compromised or buggy hub must never reach
apiKeyHelper, env, hooks or any other baseline-only key on every PC; unknown keys are dropped
and logged, and `permissions` may carry `deny` only.

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
#: Keys the hub may set in a Claude Code policy (plugins, MCP servers, locks, agent-tool denies).
HUB_KEYS = frozenset({
    "strictKnownMarketplaces",
    "extraKnownMarketplaces",
    "enabledPlugins",
    "disableSideloadFlags",
    "strictPluginOnlyCustomization",
    "managedMcpServers",
    "allowManagedMcpServersOnly",
    "permissions",
})
#: Inside `permissions` the hub may only deny (tool names of agents the team may not use).
PERMISSION_KEYS = frozenset({"deny"})


class HubUnavailable(Exception):
    """허브를 부를 수 없거나 답을 쓸 수 없음. 라우터가 503 으로 바꿔 헬퍼가 마지막 정상본을 지킨다.

    The hub cannot be reached or its answer cannot be used. The router turns this into a 503 so
    the helper keeps its last good policy.
    """

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
    """허브가 주는 이 사용자의 조각(HUB_KEYS 만). URL 없음 = {}, 그 밖의 실패 = HubUnavailable.

    This user's non-model fragment from the hub (HUB_KEYS only). {} when no URL is set;
    every other failure raises HubUnavailable.
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
        raise HubUnavailable(f"hub unreachable: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        logger.warning("policy_hub_error", status=resp.status_code, team_id=team_id)
        raise HubUnavailable(f"hub answered {resp.status_code}")
    try:
        body = resp.json()
    except ValueError:
        logger.warning("policy_hub_bad_body", reason="not json", team_id=team_id)
        raise HubUnavailable("hub answer is not JSON") from None
    fragment = body.get("managedSettings") if isinstance(body, dict) else None
    if not isinstance(fragment, dict) or any(
        not isinstance(k, str) or k.startswith("_") for k in fragment
    ):
        logger.warning(
            "policy_hub_bad_body", reason="managedSettings not a settings object", team_id=team_id,
        )
        raise HubUnavailable("hub answer is not a settings object")
    return allowed_keys_only(fragment, team_id=team_id)


def allowed_keys_only(fragment: dict, *, team_id: str = "") -> dict:
    """HUB_KEYS 밖의 키는 버리고 경고 로그, permissions 는 deny 만 남긴다.

    Drop keys outside HUB_KEYS (logged); inside permissions keep deny only.
    """
    unknown = sorted(k for k in fragment if k not in HUB_KEYS)
    if unknown:
        logger.warning("policy_hub_unknown_keys", keys=unknown, team_id=team_id)
    out = {k: v for k, v in fragment.items() if k in HUB_KEYS}
    perms = out.get("permissions")
    if perms is not None:
        if not isinstance(perms, dict):
            logger.warning(
                "policy_hub_unknown_keys", keys=["permissions (not an object)"], team_id=team_id,
            )
            del out["permissions"]
        else:
            extra = sorted(k for k in perms if k not in PERMISSION_KEYS)
            if extra:
                logger.warning(
                    "policy_hub_unknown_keys",
                    keys=[f"permissions.{k}" for k in extra],
                    team_id=team_id,
                )
            out["permissions"] = {k: v for k, v in perms.items() if k in PERMISSION_KEYS}
    return out


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
