# Copyright 2026 Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""Regression: 세 서비스가 부팅 로그에 Redis(ElastiCache) auth token 을 평문으로 남기던 결함.

Bug: `logger.info("redis_mode", mode=..., url=settings.redis_url)` — 진단용으로 URL 을
넣었는데, 런타임의 REDIS_URL 은 **비밀번호를 포함한 완성형**이다. helm 이

    rediss://:$(REDIS_PASSWORD)@<host>:<port>/<db>
    (deployment/charts/llm-gateway/templates/_helpers.tpl:149)

로 렌더하고 K8s 가 컨테이너 시작 시 `$(REDIS_PASSWORD)` 를 Secret 값
(terraform elasticache-valkey 모듈의 `random_password.auth_token`) 으로 치환하기
때문이다. 그래서 pod 이 뜰 때마다(스케일아웃/롤아웃/OOM 재시작 포함) Valkey 비밀번호가
그대로 CloudWatch Logs 에 적혔다 — dev 에서 실제로 확인됨. 로그 읽기 권한은 Redis
접근 권한보다 훨씬 넓게 뿌려져 있으므로 권한 상승 경로가 된다.

누출 지점 5곳 / 3서비스: gateway-proxy(3), cost-recorder-worker(1), notification-worker(1).
admin-api 는 URL 을 로그하지 않아 무관하다.

Fix: 각 서비스의 redis_client.py 에 `_redact_credentials` (userinfo 구간만 제거,
host/port/db 는 진단용으로 유지). db/env.py:49 의 DB_URL 리댁션과 같은 규칙이며,
같은 결함 부류의 형제 테스트가 test_high_migration_log_credential_leak.py 다.

이 테스트는 문자열 grep 이 아니라
  (1) 각 파일의 `_redact_credentials` 를 AST 로 떼어내 **실제로 실행**해 동작을 검증하고,
  (2) `logger.*` 호출의 인자를 AST 로 훑어 리댁션을 거치지 않은 URL 전달을 잡는다.
주석·문자열에 우연히 들어간 패턴이나 스코프 밖 이름에 속지 않기 위해서다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # regression -> tests -> gateway-proxy -> root

# REDIS_URL 을 로그로 남기는 서비스들. admin-api 는 로그하지 않으므로 목록에 없다 —
# 나중에 로그를 추가한다면 여기에도 추가해야 이 가드가 그 파일을 본다.
REDIS_CLIENTS = {
    "gateway-proxy": PROJECT_ROOT / "gateway-proxy" / "src" / "app" / "redis_client.py",
    "cost-recorder-worker": PROJECT_ROOT / "cost-recorder-worker" / "src" / "worker" / "redis_client.py",
    "notification-worker": PROJECT_ROOT / "notification-worker" / "src" / "worker" / "redis_client.py",
}

# helm 이 렌더하고 K8s 가 치환한 런타임 URL 과 **같은 모양**의 합성 픽스처.
#
# ⚠️ 실제 값을 여기에 넣지 말 것. 이 파일은 커밋되므로 실 AUTH 토큰을 쓰면 이 테스트가
#    막으려는 바로 그 유출을 스스로 저지른다. 그래서 비밀번호는 한눈에 가짜임을 알 수 있는
#    문자열이고 호스트도 존재하지 않는 것이다(실 dev 엔드포인트 식별자와 다르다).
#    리댁션 동작 검증에 필요한 건 값의 진짜 여부가 아니라 `://:<pw>@` 라는 **모양**뿐이다.
LIVE_SHAPE = (
    "rediss://:NOT-A-REAL-PASSWORD-fixture@"
    "lgw-dev-valkey.abc123.serverless.apne2.cache.amazonaws.com:6379/0"
)
LIVE_SECRET = "NOT-A-REAL-PASSWORD-fixture"


def _load_redactor(path: Path):
    """redis_client.py 에서 _redact_credentials 만 떼어내 실행 가능한 함수로 만든다.

    모듈째 import 하면 각 서비스의 config/settings 와 redis 패키지가 필요하고
    (cost-recorder-worker 의 `worker.config` 는 gateway-proxy 의 sys.path 에 없다),
    서비스별 테스트 하네스가 갈라진다. 함수 하나만 컴파일해 실행한다.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "_redact_credentials":
            module = ast.Module(body=[node], type_ignores=[])
            ns: dict = {"re": re}
            exec(compile(module, str(path), "exec"), ns)  # noqa: S102 - 테스트 전용
            return ns["_redact_credentials"]
    pytest.fail(
        f"{path} 에 _redact_credentials 가 없다 — 이 서비스는 REDIS_URL 을 "
        f"리댁션 없이 로그할 수 있다"
    )


def test_every_service_with_redis_logging_is_covered():
    """대조군 — 검사 대상 파일이 실제로 존재하는지. 경로가 틀리면 아래가 전부 공허해진다."""
    assert REDIS_CLIENTS, "검사 대상이 비었다"
    missing = {name: str(p) for name, p in REDIS_CLIENTS.items() if not p.exists()}
    assert not missing, f"경로가 바뀌었다 — 가드가 아무것도 검사하지 않는다: {missing}"


@pytest.mark.parametrize("service", sorted(REDIS_CLIENTS))
@pytest.mark.parametrize(
    ("url", "secret"),
    [
        (LIVE_SHAPE, LIVE_SECRET),
        # 유저명이 붙는 형태(ACL 사용 시)
        ("rediss://default:S3cr3tPw@valkey.internal:6379/0", "S3cr3tPw"),
        # 비밀번호에 인코딩되지 않은 @ 가 섞여도 마지막 @ 까지 지워야 한다
        ("redis://:pw@with@valkey.internal:6379/0", "pw@with"),
        # random_password 가 만들어내는 특수문자 조합
        ("rediss://:a1-_=+b2@valkey.internal:6379/1", "a1-_=+b2"),
    ],
)
def test_redaction_removes_the_auth_token(service: str, url: str, secret: str):
    redact = _load_redactor(REDIS_CLIENTS[service])
    out = redact(url)
    assert secret not in out, f"{service}: auth token 이 남았다: {out}"
    assert "<redacted>" in out, f"{service}: 리댁션 표시가 없다: {out}"


@pytest.mark.parametrize("service", sorted(REDIS_CLIENTS))
def test_redaction_keeps_host_and_db_for_diagnostics(service: str):
    """진단 가치는 유지 — 어느 엔드포인트/DB 에 붙었는지는 보여야 한다.

    이게 없으면 "URL 을 통째로 지운다" 는 구현도 통과해 버리고, cluster/standalone
    오배선을 로그로 진단할 수 없게 된다.
    """
    redact = _load_redactor(REDIS_CLIENTS[service])
    out = redact(LIVE_SHAPE)
    assert "lgw-dev-valkey.abc123.serverless.apne2.cache.amazonaws.com:6379/0" in out, out
    assert out.startswith("rediss://"), out


@pytest.mark.parametrize("service", sorted(REDIS_CLIENTS))
def test_redaction_handles_a_slash_in_the_auth_token(service: str):
    """⚠️ ElastiCache AUTH 토큰에 `/` 가 있을 수 있다.

    예전 정규식 `://[^/]*@` 는 그 입력에서 매치 자체가 실패해 **전체 URL 을 그대로**
    로그에 남겼다 — 리댁션이 조용히 no-op 하는, 가장 나쁜 실패 방식이다.
    """
    redact = _load_redactor(REDIS_CLIENTS[service])
    for token in ("pa/ss", "trailing/", "sl/a/sh"):
        url = f"rediss://:{token}@lgw.example.internal:6379/0"
        out = redact(url)
        assert token not in out, f"{service}: `/` 포함 토큰이 그대로 남았다: {out}"
        assert "<redacted>" in out, f"{service}: 리댁션이 동작하지 않았다: {out}"
        assert "lgw.example.internal:6379/0" in out, (
            f"{service}: 진단 정보가 사라졌다: {out}"
        )


@pytest.mark.parametrize("service", sorted(REDIS_CLIENTS))
def test_redaction_is_noop_without_credentials(service: str):
    """로컬/도커컴포즈의 비밀번호 없는 URL 은 그대로 남아야 한다(기본값 형태)."""
    redact = _load_redactor(REDIS_CLIENTS[service])
    url = "redis://redis:6379/0"
    assert redact(url) == url


@pytest.mark.parametrize("service", sorted(REDIS_CLIENTS))
def test_no_logger_call_passes_the_url_unredacted(service: str):
    """logger.* 호출이 redis_url 을 넘길 때는 반드시 리댁션을 거쳐야 한다.

    AST 로 호출 노드를 찾아 각 인자 표현식만 본다 — 주석이나 docstring 에 들어간
    `settings.redis_url` 에 속지 않는다.
    """
    path = REDIS_CLIENTS[service]
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)

    logger_calls = 0
    offenders: list[str] = []

    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        target = node.func.value
        if not (isinstance(target, ast.Name) and target.id in {"logger", "log"}):
            continue
        if node.func.attr not in {"debug", "info", "warning", "error", "exception", "critical"}:
            continue
        logger_calls += 1

        for arg in list(node.args) + [kw.value for kw in node.keywords]:
            segment = ast.get_source_segment(source, arg) or ast.dump(arg)
            mentions_url = re.search(r"\b(redis_url|REDIS_URL)\b", segment) is not None
            if mentions_url and "_redact_credentials" not in segment:
                offenders.append(f"{path.name}: {segment}")

    assert not offenders, (
        f"{service}: REDIS_URL 을 리댁션 없이 로그하는 호출이 있다 — pod 부팅마다 "
        f"Valkey auth token 이 CloudWatch 에 적힌다: {offenders}"
    )
    # 대조군 — logger 호출을 하나도 못 찾았다면 위 단정은 공허하다(파서/이름이 바뀐 것).
    assert logger_calls >= 1, (
        f"{service}: logger 호출을 하나도 찾지 못했다 — 이 가드가 아무것도 검사하지 않는다"
    )
