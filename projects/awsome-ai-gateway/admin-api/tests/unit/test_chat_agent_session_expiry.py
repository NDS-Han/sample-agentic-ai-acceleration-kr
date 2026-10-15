# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

"""R17 — chat_agent.post_message 의 세션 만료/슬라이딩 갱신 회귀.

claude R17 검증 #1: expires_at 슬라이딩 갱신이 `timedelta` 임포트 없이 쓰여
**모든** 챗 메시지가 NameError 500 으로 죽는 상태였다(유닛 커버리지 부재로
스위트 통과). 이 테스트는 만료 경로(410)와 갱신 UPDATE 발행을 직접 검증한다.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.routers import chat_agent
from app.routers.chat_agent import post_message


class _Row:
    def __init__(self, user_id, expires_at):
        self.user_id = user_id
        self.expires_at = expires_at


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _Session:
    """session.execute 를 호출된 SQL 로 기록하는 대역.

    첫 SELECT 만 세션 행을 돌려주고 이후 호출은 기록만 한다. 만료 갱신
    UPDATE 이후 INSERT 메시지 실행 시 RuntimeError 를 던져 AgentCore 경로로
    더 진행하지 않게 한다(테스트 관심사는 만료 분기까지).
    """

    def __init__(self, row):
        self.calls: list[str] = []
        self.row = row
        self.committed = False

    async def execute(self, stmt, *a, **k):
        sql = str(stmt)
        self.calls.append(sql)
        if "SELECT user_id, expires_at" in sql:
            return _Result(self.row)
        if "INSERT INTO chat_agent.messages" in sql:
            raise RuntimeError("stop — beyond expiry branch")
        return _Result(None)

    async def commit(self):
        self.committed = True


def _admin(user_id):
    return SimpleNamespace(user_id=user_id)


@pytest.fixture(autouse=True)
def _runtime_arn(monkeypatch):
    # 모듈 상수 — 테스트 환경에는 설정이 없어 503 으로 먼저 죽으므로 패치.
    monkeypatch.setattr(chat_agent, "AGENTCORE_RUNTIME_ARN", "arn:aws:test")


@pytest.mark.asyncio
async def test_expired_session_returns_410():
    uid = uuid.uuid4()
    sess = _Session(_Row(uid, datetime.now(timezone.utc) - timedelta(hours=1)))
    with pytest.raises(HTTPException) as ei:
        await post_message(
            str(uuid.uuid4()),
            chat_agent.MessageCreate(content="hi"),
            admin=_admin(uid),
            session=sess,
        )
    assert ei.value.status_code == 410


@pytest.mark.asyncio
async def test_active_session_slides_expiry_and_runs_update():
    uid = uuid.uuid4()
    sess = _Session(_Row(uid, datetime.now(timezone.utc) + timedelta(hours=1)))
    with pytest.raises(RuntimeError, match="beyond expiry"):
        await post_message(
            str(uuid.uuid4()),
            chat_agent.MessageCreate(content="hi"),
            admin=_admin(uid),
            session=sess,
        )
    # 슬라이딩 갱신 UPDATE 가 발행됐어야 한다 — 여기서 NameError(timedelta) 가
    # 나면 이 테스트가 R17 시절 그대로 죽는다.
    assert any("SET expires_at" in sql for sql in sess.calls)
