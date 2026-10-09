# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Identify the calling client (Claude Code / Cowork / Codex) from request headers.

Claude Code and Cowork both send a `claude-cli/X.Y.Z` User-Agent prefix; the ONLY
reliable differentiator is the surface token in parentheses. Cowork is checked
first so it is never misclassified as claude-code. See COWORK-vs-CLAUDE-CODE.md §B.

Codex (OpenAI Codex CLI on Bedrock) is a DIFFERENT client family — it speaks the
OpenAI Responses API, not the Anthropic `claude-cli/` wire, and self-identifies via
the `originator` header (default `codex_cli_rs`) and a `codex` User-Agent token.
It is checked before the `claude-cli/` fallback; the families don't overlap.

This is a pure function with no I/O — the UA/originator are untrusted, spoofable
tags used for logging/analytics + routing-profile selection, NOT an authorization
signal. The trust axis stays VK + team/org + allowed_clients allow-list; a missed
classification only degrades to 'other' (no routing profile), never a privilege.

⚠️ 위협 모델 메모 (R3-7): "권한 신호가 아니다" 라는 문구는 스푸핑의 영향이
   *없다* 는 뜻이 아니다. 스푸핑한 헤더 하나로 실제로 바뀌는 것:

   1. 라우팅 프로파일 선택 — 예: `anthropic-client-platform: desktop_app` 하나로
      claude-code 호출자가 cowork 프로파일로 분류돼 **크로스어카운트
      assume-role 라우팅**(다른 AWS 계정의 모델)을 탈 수 있다.
   2. per-app 예산 버킷 — 사용량이 `app:{client}` 키로 잡히므로 스푸핑 시
      실제 앱의 예산이 아니라 가장한 앱의 예산을 소진/우회한다.
   3. `allowed_clients` 게이트도 이 헤더를 기준으로 판정한다 — 다만 이 게이트는
      "허용 목록" 이라 스푸핑으로 *권한 상승* 은 안 된다(없는 앱을 가장해도
      그 앱이 허용된 모델만 탄다). 벗어나는 방향은 "자기가 허용된 모델을 다른
      앱 이름으로 부르는 것" 뿐.

   합쳐서: 스푸핑으로 *더 많은 모델* 을 얻지는 못하지만, **어느 계정에서
   서빙되고 어느 예산 버킷을 쓰는지** 는 공격자가 고를 수 있다. per-app 예산이
   경제적 경계로 의존할 거라면 client 는 헤더가 아니라 VK 바인딩(키 ↔ 허용 앱)
   으로 바꿔야 한다 — 그건 스키마/발급 플로우를 건드리는 설계 변경이라 이
   문서에만 기록하고 별도 작업으로 분리한다.
"""

from __future__ import annotations

CLIENT_CLAUDE_CODE = "claude-code"
CLIENT_COWORK = "cowork"
CLIENT_CODEX = "codex"
CLIENT_OTHER = "other"


def identify_client(headers: dict[str, str]) -> str:
    """Return 'claude-code', 'cowork', 'codex', or 'other' from request headers.

    `headers` keys may be any case; we normalize to lowercase.
    """
    h = {k.lower(): v for k, v in headers.items()}
    ua = h.get("user-agent", "")
    platform = h.get("anthropic-client-platform", "")
    # OpenAI Codex CLI tags every request with an originator header (default
    # `codex_cli_rs`); newer/older builds may also surface a `codex` UA token.
    originator = h.get("originator", "")

    # Cowork first — both Anthropic clients carry the claude-cli/ prefix.
    if (
        platform == "desktop_app"
        or "claude-desktop-3p" in ua
        or "local-agent" in ua
        or ("Electron/" in ua and "Claude/" in ua)  # Cowork healthcheck UA
    ):
        return CLIENT_COWORK
    # Codex — distinct OpenAI client family (Responses API). Match the originator
    # header first (most reliable), then a UA token as a fallback.
    if (
        originator.startswith("codex")
        or "codex_cli_rs" in ua
        or ua.startswith("codex/")
        or "codex-cli" in ua
    ):
        return CLIENT_CODEX
    if ua.startswith("claude-cli/"):
        return CLIENT_CLAUDE_CODE
    return CLIENT_OTHER
