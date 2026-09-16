# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""라이브 스택이 필요한 integration 테스트의 공용 게이트.

## 왜 필요한가

``pytest`` 를 그냥 돌리면 스위트가 **RED** 였다 — 3 failed + 10 errors. 원인은
``test_count_tokens_endpoint.py`` / ``test_models_endpoint.py`` /
``test_openai_compat_e2e.py`` 세 파일이 localhost 의 gateway-proxy(8000) + admin-api(8080)
+ seed 된 DB 를 전제하는데 **skip 게이트가 아예 없었다**는 것이다. 스택이 없으면
``httpx.ConnectError`` 로 죽는다.

이건 단순한 잡음이 아니다 — CI 를 붙이는 순간 첫날부터 빨강이라, "빨강은 원래 그런 것"이
되어 진짜 회귀를 가린다. 레포엔 이미 옳은 관용구가 있었다(``test_rate_limit_cluster.py`` 의
``pytestmark = [skipif(not CLUSTER_URL, ...)]``, ``test_invocation_logging_live.py`` 의
``RUN_INVOCATION_LOG_LIVE``) — 이 파일은 그 관용구를 스택 전체로 일반화한다.

## 게이트 방식: 명시 opt-in 이 아니라 **실제 도달성 probe**

Redis Cluster 게이트는 "URL 이 설정됐나"만 본다. 스택은 그걸로 부족하다 — compose 를 띄웠는지
아닌지가 진짜 조건이고, 사람이 env 를 따로 세팅해야 하면 스택이 떠 있어도 테스트가 안 돈다
(= 조용히 미검증). 그래서 ``/health`` 를 짧은 타임아웃으로 두드려보고 안 열리면 skip 한다.

## ⚠️ prod 안전장치

이 테스트들은 ``POST {ADMIN_URL}/internal/test/issue-key`` 로 **실제 사용 가능한 virtual key
를 발급**한다. 그래서 GATEWAY_URL/ADMIN_API_URL 이 localhost 가 아니면 **거부**한다.
단순한 기우가 아니다 — ``/internal/*`` 의 dev 가드는 ``APP_ENV == "production"`` 을 보는데
prod 는 ``APP_ENV="prod"`` 로 떠 있어서 가드가 무력했던 전력이 있다(즉 prod 에서도 무인증
VK 발급이 열려 있었다). 실수로 prod 를 가리키면 테스트가 조용히 성공하는 대신 여기서 멈춘다.
비-로컬 호스트를 정말 쓰려면 ``ALLOW_NONLOCAL_INTEGRATION_TARGET=1`` 로 명시 opt-in 한다.
"""

from __future__ import annotations

import os
import socket
from urllib.parse import urlparse

import pytest

GATEWAY_URL = os.environ.get("GATEWAY_URL", "http://localhost:8000")
ADMIN_URL = os.environ.get("ADMIN_API_URL", "http://localhost:8080")

# 루프백으로 간주하는 호스트. IPv6 루프백과 compose 내부 서비스명도 포함
# (compose 네트워크 안에서 돌릴 때는 호스트가 서비스명이다).
_LOCAL_HOSTS = frozenset(
    {
        "localhost",
        "127.0.0.1",
        "::1",
        "[::1]",
        "0.0.0.0",
        "host.docker.internal",
        "gateway-proxy",
        "admin-api",
    }
)


def _host_port(url: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(url)
    return (parsed.hostname or "localhost", parsed.port or default_port)


def _is_local(url: str) -> bool:
    host, _ = _host_port(url, 0)
    return host in _LOCAL_HOSTS


def _tcp_open(host: str, port: int, timeout: float = 0.5) -> bool:
    """해당 host:port 에 TCP 연결이 되는지만 본다.

    HTTP 요청을 보내지 않는 이유: ``/health`` 경로가 서비스마다 다르고, 응답 본문 검증은
    개별 테스트의 일이다. 여기서 답해야 할 질문은 "떠 있나" 하나뿐이다.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _reachable(url: str, default_port: int, timeout: float = 0.5) -> bool:
    host, port = _host_port(url, default_port)
    return _tcp_open(host, port, timeout)


def db_unavailable(dsn: str) -> bool:
    """DSN 의 host:port 에 TCP 연결이 안 되면 True. 클래스 단위 skipif 용."""
    host, port = _host_port(dsn, 5432)
    return not _tcp_open(host, port)


def db_skip_reason(dsn: str) -> str:
    host, port = _host_port(dsn, 5432)
    return (
        f"실 Postgres 미가동({host}:{port}) — docker compose up -d postgres 후 "
        f"재실행하거나 TEST_DB_URL 로 시드된 DB 를 지정한다."
    )


def live_db_gate(dsn: str) -> list:
    """실 Postgres 가 필요한 **모듈** 이 ``pytestmark`` 에 펼쳐 쓰는 마커 목록.

    ``live_stack_gate`` 와 분리한 이유: 이 테스트들은 HTTP 스택(8000/8080)이 아니라
    **DB 한 곳**만 필요하다. 스택 게이트를 그대로 쓰면 DB 만 있는 환경에서 불필요하게
    skip 되고, 반대로 DB 게이트가 없으면 CI 에서 ``asyncpg`` connect 실패로 죽는다.

    ⚠️ 실제로 그렇게 죽었다. ``test_bedrock_e2e.py`` / ``test_router_service_db.py`` 가
    게이트 없이 ``localhost:5432`` 를 요구해 CI 에서 7 failed 였다. 로컬에서는 예전
    docker-compose 의 postgres 컨테이너가 5432 에 떠 있어서 통과했다 — 즉 스위트가
    **떠돌던 컨테이너에 의존**하고 있었고 아무도 몰랐다.

    한 파일에서 일부 클래스만 DB 를 쓰면 이 모듈 게이트 대신 ``db_unavailable`` /
    ``db_skip_reason`` 으로 그 클래스에만 ``@pytest.mark.skipif`` 를 건다.
    """
    return [
        pytest.mark.integration,
        pytest.mark.skipif(db_unavailable(dsn), reason=db_skip_reason(dsn)),
    ]


def live_stack_gate() -> list:
    """라이브 스택이 필요한 모듈이 ``pytestmark`` 에 펼쳐 쓰는 마커 목록.

    사용법::

        from tests.integration.conftest import live_stack_gate
        pytestmark = live_stack_gate()
    """
    nonlocal_allowed = os.environ.get("ALLOW_NONLOCAL_INTEGRATION_TARGET") == "1"
    targets = {"GATEWAY_URL": GATEWAY_URL, "ADMIN_API_URL": ADMIN_URL}
    nonlocal_targets = {k: v for k, v in targets.items() if not _is_local(v)}

    if nonlocal_targets and not nonlocal_allowed:
        # ⚠️ skip 이 아니라 명시적 차단이다. 이 테스트들은 VK 를 발급하고 LLM 을 호출하므로
        #    잘못된 대상을 향한 채 "통과"하는 것이 가장 나쁜 결과다.
        return [
            pytest.mark.integration,
            pytest.mark.skip(
                reason=(
                    f"비-로컬 대상 {nonlocal_targets} — 이 테스트는 /internal/test/issue-key 로 "
                    "실사용 가능한 VK 를 발급하므로 로컬이 아닌 대상에는 돌리지 않는다. "
                    "정말 필요하면 ALLOW_NONLOCAL_INTEGRATION_TARGET=1"
                )
            ),
        ]

    missing = [
        name
        for name, url, port in (
            ("gateway-proxy", GATEWAY_URL, 8000),
            ("admin-api", ADMIN_URL, 8080),
        )
        if not _reachable(url, port)
    ]
    return [
        pytest.mark.integration,
        pytest.mark.skipif(
            bool(missing),
            reason=(
                f"라이브 스택 미가동({', '.join(missing)}) — docker compose up -d 후 재실행. "
                f"GATEWAY_URL={GATEWAY_URL} ADMIN_API_URL={ADMIN_URL}"
            ),
        ),
    ]
