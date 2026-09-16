# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""모델 × 앱 allow-list 가 **실 Postgres 값을 읽어** 정말로 거부하는지.

왜 실 DB 가 필요한가
--------------------
이 게이트는 한 번 완전히 무력화된 채로 배포됐다. 마이그레이션 0035, 게이트 함수(3-state
``is None`` 의미까지 정확히), 다섯 개 호출 지점, admin-api 쓰기 경로, 관리자 UI 가 모두
있었는데 **gateway-proxy 의 ORM 클래스만 컬럼을 선언하지 않았다.**
``_orm_to_schema`` 는 ``getattr(alias_row, "allowed_clients", None)`` 로 읽으므로 그
getattr 이 언제나 None 을 돌려주고, 게이트는 즉시 return 했다. 관리자 화면은 "이 앱
차단" 이라고 표시하고 admin-api 는 저장에 성공하는데, 데이터 경로에서는 모든 앱이 그
모델을 계속 호출했다. 오류도, 경고도, 로그도 없었다.

그 상태에서 기존 회귀 테스트 24건은 **전부 통과했다.** 게이트 자체를 가짜
model_config 로 호출하는 단위 테스트였고, ORM 변환 검사는 ``_orm_to_schema`` 의 kwarg
이름만 AST 로 확인했기 때문이다 — 값이 영구히 None 인 상태와 구별되지 않는다.

이 파일은 그 구멍을 원리적으로 막는다: **DB 에 값을 넣고, 실제 리졸버로 읽어, 게이트
판정을 본다.** 세 상태(NULL / 목록 / 빈 배열)가 각각 다른 결과를 내는지 확인하므로,
어느 계층이 값을 잃어버리든(ORM 컬럼 누락, ARRAY 타입 오류, 캐시 직렬화, 변환 함수)
전부 여기서 잡힌다.

⚠️ 트랜잭션을 롤백한다. 공용 proof DB 를 쓰면서 행을 남기면 뒤에 도는 테스트를 깨뜨린다.
   DDL 도 하지 않는다 — 스키마는 alembic 이 만든 그대로여야 이 검사가 의미가 있다.

실행:
    PROOF_DSN=postgresql+asyncpg://... \\
      pytest tests/regression/test_critical_model_client_scope_real_db.py
PROOF_DSN 은 ``db/init`` + ``alembic upgrade head`` 를 마친 DB 를 가리켜야 한다.
"""

from __future__ import annotations

import os
import uuid

import pytest

PROOF_DSN = os.environ.get("PROOF_DSN")

pytestmark = pytest.mark.skipif(
    not PROOF_DSN,
    reason=(
        "PROOF_DSN 미설정 — 실 Postgres 필요(가짜 DB 로는 이 결함이 원리적으로 잡히지 "
        "않는다). db/init + alembic upgrade head 를 마친 DB 를 가리킬 것."
    ),
)

#: 이 파일이 만드는 alias 접두사. 롤백하므로 남지 않지만, 실패로 중단됐을 때
#: 무엇이 남았는지 알아볼 수 있게 접두사를 붙인다.
_PREFIX = "zzproof-scope-"

#: (allowed_clients DB 값, client, 게이트 통과 기대)
_CASES = [
    (None, "codex", True),
    (None, "claude-code", True),
    (None, None, True),
    (["claude-code"], "claude-code", True),
    (["claude-code"], "codex", False),
    (["claude-code"], "cowork", False),
    # ⚠️ client 를 식별하지 못한 요청(None)은 목록에 들 수 없으므로 거부다. 통과시키면
    #    User-Agent 를 바꾸는 것만으로 allow-list 를 우회할 수 있다.
    (["claude-code"], None, False),
    (["claude-code", "codex"], "codex", True),
    # 빈 배열 = 명시적 전면 거부. 운영자가 마지막 앱의 체크를 해제하면 나오는 값이다.
    ([], "claude-code", False),
    ([], "codex", False),
    ([], None, False),
]


def _alias_for(index: int) -> str:
    return f"{_PREFIX}{index}"


async def _seed(session, actor_id: str) -> None:
    """세 상태의 alias 를 넣는다. 커밋하지 않는다(호출자가 롤백)."""
    from sqlalchemy import text

    await session.execute(
        text(
            "INSERT INTO auth.users (id, email, display_name, role, sso_subject) "
            "VALUES (:id, :email, 'scope-proof', 'ADMIN', :sub)"
        ),
        {"id": actor_id, "email": f"{actor_id}@example.invalid", "sub": actor_id},
    )
    seen: dict[str, int] = {}
    for i, (allowed, _client, _expect) in enumerate(_CASES):
        key = repr(allowed)
        if key in seen:
            continue
        seen[key] = i
        await session.execute(
            text(
                "INSERT INTO model.model_aliases "
                "(alias, provider, provider_model_id, api_format, status, created_by, "
                "allowed_clients) "
                "VALUES (:alias, 'BEDROCK', :pmid, 'ANTHROPIC_MESSAGES', 'ACTIVE', :actor, "
                # ⚠️ 리스트를 그대로 바인딩한다. Postgres 배열 리터럴 문자열('{a,b}')을
                #    주면 asyncpg 가 DataError 로 거부한다("a sized iterable container
                #    expected"). CAST 는 빈 리스트의 타입 추론을 위해 필요하다 — 없으면
                #    `[]` 의 원소 타입을 알 수 없어 실패한다.
                #
                # ⚠️ NULL 과 `{}` 가 **구별되어** 저장되는지가 이 파일의 전제다. 아래
                #    `observed` 대조군이 그것을 확인한다(구별되지 않으면 전면 거부
                #    케이스가 조용히 "제한 없음" 이 된다).
                "CAST(:allowed AS TEXT[]))"
            ),
            {
                "alias": _alias_for(i),
                "pmid": f"anthropic.scope-proof-{i}",
                "actor": actor_id,
                "allowed": allowed,
            },
        )
    # 다음 SELECT 가 이 행을 보도록 flush. 커밋은 하지 않는다.
    await session.flush()


async def test_the_column_exists_in_the_real_database():
    """⚠️ 먼저 확인한다 — 없으면 아래 검사들이 왜 실패하는지 알 수 없다.

    이 컬럼은 두 경로로 들어온다: 마이그레이션 0035, 그리고 ``db/init`` 의 idempotent
    ALTER. 둘 다 필요하다 — ORM 이 컬럼을 선언하므로 model_aliases 의 **모든 SELECT** 에
    들어가고, 컬럼이 없는 DB 에서는 allow-list 기능만이 아니라 추론 경로 전체가
    UndefinedColumn 으로 죽는다.
    """
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            row = (
                await conn.execute(
                    text(
                        "SELECT data_type, is_nullable FROM information_schema.columns "
                        "WHERE table_schema='model' AND table_name='model_aliases' "
                        "AND column_name='allowed_clients'"
                    )
                )
            ).first()
        assert row is not None, (
            "model.model_aliases.allowed_clients 가 DB 에 없다 — alembic 이 0035 에 "
            "닿지 않았거나 db/init 의 ALTER 가 빠졌다. 이 상태에서 ORM 은 컬럼을 "
            "선언하므로 모든 모델 조회가 UndefinedColumn 으로 죽는다."
        )
        assert row[0] == "ARRAY", f"타입이 ARRAY 가 아니다: {row[0]}"
        assert row[1] == "YES", (
            "NOT NULL 이다 — NULL(제한 없음)을 표현할 수 없어 3-state 가 붕괴한다"
        )
    finally:
        await engine.dispose()


async def test_real_db_values_reach_the_gate():
    """⚠️ 이 파일의 핵심. DB → ORM → 스키마 → 게이트가 끝까지 이어지는지.

    ORM 컬럼 선언을 지우면 세 상태가 모두 None 으로 읽혀 **거부 케이스 6건이 전부
    통과한다**(실측). 그것이 실제로 배포된 상태였다.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.services.router_service import RouterService, check_client_model_scope

    engine = create_async_engine(PROOF_DSN)
    actor_id = str(uuid.uuid4())
    failures: list[str] = []
    observed: set[str] = set()
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            try:
                await _seed(session, actor_id)
                rs = RouterService()
                for i, (allowed, client, should_pass) in enumerate(_CASES):
                    alias = _alias_for(
                        next(
                            j
                            for j, (a, _c, _e) in enumerate(_CASES)
                            if repr(a) == repr(allowed)
                        )
                    )
                    # redis=None → DB 경로. 캐시를 끼우면 무엇을 검사하는지 흐려진다.
                    cfg = await rs.resolve_bedrock_model(None, session, alias)
                    got = getattr(cfg, "allowed_clients", "MISSING-ATTR")
                    observed.add(repr(got))
                    try:
                        check_client_model_scope(cfg, client)
                        passed = True
                    except PermissionError:
                        passed = False
                    if passed != should_pass:
                        failures.append(
                            f"[{i}] db={allowed!r} client={client!r}: "
                            f"게이트={'통과' if passed else '거부'} "
                            f"(기대 {'통과' if should_pass else '거부'}), "
                            f"스키마가 본 값={got!r}"
                        )
            finally:
                # 공용 proof DB 를 더럽히지 않는다.
                await session.rollback()
    finally:
        await engine.dispose()

    # 대조군 — 세 상태가 서로 **다른** 값으로 읽혔는가. 전부 None 이면 위 단정 중
    # 통과 기대 케이스만 맞아떨어져 검사가 반쯤 공허해진다.
    assert len(observed) >= 3, (
        f"스키마가 본 값의 종류가 {len(observed)}가지뿐이다({sorted(observed)}) — "
        "세 상태(NULL/목록/빈배열)가 구별되지 않는다. ORM 컬럼이나 ARRAY 타입 문제다."
    )
    assert failures == [], "실 DB 값이 게이트에 반영되지 않는다:\n  " + "\n  ".join(failures)


async def test_nothing_was_left_behind():
    """롤백이 실제로 됐는지. 남으면 뒤에 도는 테스트가 알 수 없는 이유로 깨진다."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(PROOF_DSN)
    try:
        async with engine.connect() as conn:
            n = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM model.model_aliases WHERE alias LIKE :p"
                    ),
                    {"p": f"{_PREFIX}%"},
                )
            ).scalar_one()
        assert n == 0, f"{n}개 alias 가 남았다 — 롤백이 되지 않았다"
    finally:
        await engine.dispose()
