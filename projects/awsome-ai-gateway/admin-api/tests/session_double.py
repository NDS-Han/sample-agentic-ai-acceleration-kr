# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""AsyncSession 대역에 savepoint(`begin_nested`) 를 달아주는 헬퍼.

⚠️ 왜 필요한가: 실제 SQLAlchemy 의 `AsyncSession.begin_nested()` 는 **동기 호출**이고
`AsyncSessionTransaction`(async context manager)을 돌려준다 — `await` 하지 않는다.
그런데 테스트가 session 을 `AsyncMock()` 으로 만들면 `begin_nested()` 가 코루틴을
돌려주므로 `async with` 가

    TypeError: 'coroutine' object does not support the asynchronous context manager protocol

로 터진다. 프로덕션 코드가 틀린 게 아니라 대역이 실물과 다른 것이다.

`__aexit__` 가 예외를 삼키지 않게(False 반환) 두는 것이 중요하다 — savepoint 안에서 난
IntegrityError 는 재시도 로직이 잡아야 하므로 밖으로 전파돼야 한다.
"""

from __future__ import annotations

from unittest.mock import MagicMock


class _SavepointDouble:
    """`async with session.begin_nested():` 를 만족하는 최소 대역."""

    def __init__(self) -> None:
        self.entered = 0

    async def __aenter__(self):
        self.entered += 1
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        # False = 예외를 삼키지 않는다. True 로 두면 재시도 테스트가 공허해진다.
        return False


def wire_savepoint(session) -> list[_SavepointDouble]:
    """session 대역에 `begin_nested` 를 달고, 생성된 savepoint 목록을 돌려준다.

    반환값의 길이로 savepoint 진입 횟수(=재시도 횟수)를 검사할 수 있다.
    """
    created: list[_SavepointDouble] = []

    def _begin_nested():
        sp = _SavepointDouble()
        created.append(sp)
        return sp

    session.begin_nested = MagicMock(side_effect=_begin_nested)
    return created
