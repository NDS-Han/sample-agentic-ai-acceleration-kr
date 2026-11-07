"""The server half of the policy: fetch this user's fragment from the gateway.

The VK comes from api-key-helper's cache (`vk-cache.json`), read only — api-key-helper
refreshes and rewrites that file, and two writers would race. Two gateway-cli generations
keep it in different places (see vk_paths_for), so every candidate is tried in turn. The
last good answer is cached so that a later failure still yields that policy.
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable, Sequence
from pathlib import Path

from policy_helper import __version__

FETCH_TIMEOUT_SECONDS = 3.0  # well inside policyHelper.timeoutMs (10 s)


def merge(baseline: dict, fragment: dict) -> dict:
    """조각이 이긴다. 중첩 객체(env 등)는 한 단계만 합치고, 조각의 null 은 그 키를 지운다.

    null 이 필요한 이유: 엄격한 기본값(예: strictKnownMarketplaces: []) 을 서버 정책이
    풀어 주려면 키 자체를 없애야 하는데, JSON 으로 "지워라" 를 말할 다른 방법이 없다.

    Fragment wins; nested objects (e.g. env) are merged one level deep, and a null in
    the fragment removes that key.

    Why null: to lift a strict default (e.g. strictKnownMarketplaces: []) the server
    policy must remove the key itself, and JSON has no other way to say "remove".
    """
    merged = dict(baseline)
    for key, value in fragment.items():
        if value is None:
            merged.pop(key, None)
        elif isinstance(value, dict) and isinstance(merged.get(key), dict):
            inner = dict(merged[key])
            for inner_key, inner_value in value.items():
                if inner_value is None:
                    inner.pop(inner_key, None)
                else:
                    inner[inner_key] = inner_value
            merged[key] = inner
        else:
            merged[key] = value
    return merged


def resolve_fragment(
    baseline: dict,
    *,
    vk_path: Path | Sequence[Path],
    cache_path: Path,
    fetch: Callable[[str, str, float], dict],
    now: float,
    note: Callable[[str], None],
) -> dict:
    """This user's fragment: from the gateway if possible, else the last good one, else {}.

    Never raises for expected trouble — every problem becomes a note and a fallback.
    """
    env = baseline.get("env") or {}
    base_url = env.get("ANTHROPIC_BASE_URL")
    if not isinstance(base_url, str) or not base_url:
        note("baseline has no env.ANTHROPIC_BASE_URL; skipping server policy")
        return {}
    url = base_url.rstrip("/") + "/v1/policy"

    vk = _usable_vk(vk_path, env.get("GATEWAY_CLI_GATEWAY_URL"), now, note)
    if vk is not None:
        try:
            body = fetch(url, vk, FETCH_TIMEOUT_SECONDS)
        except Exception as exc:  # noqa: BLE001 — any network/HTTP failure -> cache
            note(f"policy fetch failed ({type(exc).__name__}: {exc}); using cached policy")
        else:
            fragment = _valid_fragment(body, note)
            if fragment is not None:
                _save_cache(cache_path, url, body, note)
                return fragment
            note("policy answer was not a usable managedSettings object; using cached policy")
    return _cached_fragment(cache_path, url, note)


def _usable_vk(vk_path: Path | Sequence[Path], admin_api_url, now: float, note) -> str | None:
    """후보 파일을 순서대로 보아 처음 쓸 수 있는 VK 를 돌려준다.

    없는 후보는 조용히 건너뛰고(전부 없을 때만 메모 한 줄), 쓸 수 없는 후보(만료·다른
    배포·읽기 실패)는 기억해 두고 다음을 본다. 그 문제는 쓸 수 있는 후보가 하나도 없을
    때만 메모로 남긴다.

    The first usable VK among the candidate files, in order.

    A missing candidate is skipped silently (one note if every one is missing); an unusable
    one (expired, other deployment, unreadable) is remembered and the next tried, and its
    problem is noted only when no candidate was usable.
    """
    paths = [vk_path] if isinstance(vk_path, Path) else list(vk_path)
    problems: list[str] = []
    missing = 0
    for path in paths:
        try:
            vk = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            missing += 1
            continue
        except (OSError, ValueError) as exc:
            problems.append(f"VK cache unreadable ({exc}); using cached policy")
            continue
        problem = _vk_problem(vk, admin_api_url, now)
        if problem is None:
            return vk["virtual_key"]
        problems.append(problem)
    if missing == len(paths):
        note("no VK cached yet (not logged in, or no request made); using cached policy")
    for problem in problems:
        note(problem)
    return None


def _vk_problem(vk, admin_api_url, now: float) -> str | None:
    """이 캐시된 VK 를 보낼 수 없는 이유. 보낼 수 있으면 None.

    Why this cached VK cannot be sent, or None when it can.
    """
    if not isinstance(vk, dict) or not isinstance(vk.get("virtual_key"), str):
        return "VK cache has no virtual_key; using cached policy"
    if _same_url(vk.get("admin_api_url"), admin_api_url) is False:
        return "VK belongs to another deployment; not sending it"
    try:
        expired = float(vk.get("expires_at", 0)) <= now
    except (TypeError, ValueError):
        expired = True
    if expired:
        return "VK expired; using cached policy until api-key-helper renews it"
    return None


def _same_url(a, b) -> bool | None:
    """False only when both are known and differ; None when either is missing."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return None
    return a.rstrip("/").lower() == b.rstrip("/").lower()


#: Keys a server fragment may set: the gateway's model keys and the hub's policy keys. Everything
#: else (apiKeyHelper, env, statusLine, hooks, policyHelper ...) belongs to the admin-installed
#: baseline only — a compromised server must not be able to change it on every PC.
FRAGMENT_KEYS = frozenset({
    "availableModels", "enforceAvailableModels", "model",
    "strictKnownMarketplaces", "extraKnownMarketplaces", "enabledPlugins",
    "disableSideloadFlags", "strictPluginOnlyCustomization",
    "managedMcpServers", "allowManagedMcpServersOnly", "permissions",
})
#: Inside `permissions` a fragment may only deny.
PERMISSION_KEYS = frozenset({"deny"})


def _valid_fragment(body, note=None) -> dict | None:
    """The usable fragment of a server answer, restricted to FRAGMENT_KEYS; None if unusable."""
    fragment = body.get("managedSettings") if isinstance(body, dict) else None
    if not isinstance(fragment, dict):
        return None
    if any(not isinstance(k, str) or k.startswith("_") for k in fragment):
        return None
    unknown = sorted(k for k in fragment if k not in FRAGMENT_KEYS)
    if unknown and note:
        note(f"ignoring keys a policy fragment may not set: {', '.join(unknown)}")
    out = {k: v for k, v in fragment.items() if k in FRAGMENT_KEYS}
    perms = out.get("permissions")
    if perms is not None:
        if not isinstance(perms, dict):
            out.pop("permissions")
            if note:
                note("ignoring permissions: not an object")
        else:
            extra = sorted(k for k in perms if k not in PERMISSION_KEYS)
            if extra and note:
                note(f"ignoring permissions keys a fragment may not set: {', '.join(extra)}")
            out["permissions"] = {k: v for k, v in perms.items() if k in PERMISSION_KEYS}
    return out


def _save_cache(cache_path: Path, url: str, body: dict, note) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"source": url, "response": body}), encoding="utf-8")
        tmp.replace(cache_path)  # atomic: a crash never leaves a half-written cache
    except OSError as exc:
        note(f"could not save policy cache ({exc})")


def _cached_fragment(cache_path: Path, url: str, note) -> dict:
    try:
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        note("no cached policy yet; baseline only")
        return {}
    except (OSError, ValueError) as exc:
        note(f"policy cache unreadable ({exc}); ignoring it")
        return {}
    if not isinstance(cached, dict) or cached.get("source") != url:
        note("policy cache is from another gateway; ignoring it")
        return {}
    fragment = _valid_fragment(cached.get("response"), note)
    return fragment if fragment is not None else {}



def http_fetch(url: str, vk: str, timeout: float) -> dict:
    """GET the policy with the VK. Non-200 and network errors raise OSError."""
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {vk}",
            "Accept": "application/json",
            "User-Agent": f"policy-helper/{__version__}",
        },
    )
    # The URL comes from the admin-only baseline (env.ANTHROPIC_BASE_URL), not from user input.
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _data_dir(app: str, platform: str, env, home: Path) -> Path:
    if platform == "win32":
        return Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / app
    if platform == "darwin":
        return home / "Library" / "Application Support" / app
    return Path(env.get("XDG_DATA_HOME") or home / ".local" / "share") / app


def vk_path_for(platform: str, env, home: Path) -> Path:
    """Where gateway-cli-v2's api-key-helper keeps the VK (same rules as its paths.py)."""
    override = env.get("GATEWAY_CLI_VK_CACHE")
    if override:
        return Path(override)
    data_dir = env.get("GATEWAY_CLI_DATA_DIR")
    return (Path(data_dir) if data_dir else _data_dir("gateway-cli", platform, env, home)) / "vk-cache.json"


def vk_paths_for(platform: str, env, home: Path) -> list[Path]:
    """VK 가 캐시돼 있을 수 있는 모든 위치, 가능성 높은 순.

    gateway-cli-v2(Windows 설치기, conda env) 는 플랫폼 데이터 폴더 아래에 쓰고, 옛
    gateway-cli(Mac, pip install) 는 ~/.gateway-cli/vk-cache.json 에 쓴다. 둘 다
    GATEWAY_CLI_VK_CACHE 를 따르므로 그 값이 있으면 후보는 정확히 하나다.

    Every place a VK may be cached, most likely first.

    gateway-cli-v2 (Windows installer, conda env) writes under the platform data dir;
    the legacy gateway-cli (Mac, pip install) writes ~/.gateway-cli/vk-cache.json. Both
    honour GATEWAY_CLI_VK_CACHE, so with it set there is exactly one candidate.
    """
    primary = vk_path_for(platform, env, home)
    if env.get("GATEWAY_CLI_VK_CACHE"):
        return [primary]
    return [primary, home / ".gateway-cli" / "vk-cache.json"]


def cache_path_for(platform: str, env, home: Path) -> Path:
    """Our own folder, so `gateway-cli clear` / uninstall neither touch nor miss it."""
    return _data_dir("policy-helper", platform, env, home) / "policy-cache.json"
