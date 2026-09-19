"""The connection retry, exercised with a REAL botocore client against a local server.

The unit tests next door use a mocked client, so they cannot tell whether botocore really
raises what we catch, whether dropping the pooled connections breaks the client, or what
happens when SEVERAL pooled connections are dead at once. This file answers those with real
sockets: a small HTTP server that behaves like the path to Bedrock does after an idle period —
a kept-alive connection looks healthy until the next request is sent on it, and is then closed
without an answer (``ConnectionClosedError`` in botocore).

What is pinned here:
- one dead pooled connection → 200 on the retry, for invoke_model, converse, count_tokens and
  the streaming call;
- 5 and 12 pooled connections that went stale TOGETHER, hit by a burst of concurrent requests
  → all 200 (the pool reset before the retry is what makes a single retry enough — measured
  without it: 3 of 25 requests still failed with five stale connections);
- a request that is in flight while another request resets the pool still completes;
- an error the server ANSWERED with (429) and a read timeout are not retried — the request
  may have reached Bedrock, a retry could bill twice;
- when every connection dies, the caller gets one 502 after exactly two attempts.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time

import boto3
import pytest
from botocore.config import Config

from app.providers.bedrock_adapter import BedrockAdapter

OK = json.dumps({"content": [{"type": "text", "text": "hi"}],
                 "usage": {"input_tokens": 3, "output_tokens": 2}}).encode()


class FakeBedrock:
    """HTTP/1.1 keep-alive server. ``policy(conn_no, req_no_on_conn, path)`` decides per request:
    "ok" | "drop" (close without answering) | "429" | "hang" | ("slow", seconds).

    ``stale_after`` models the real thing: a connection that sat idle for longer than this many
    seconds is dead when it is reused (its request is dropped without an answer); a connection
    that is being used keeps working. Fresh connections are never affected."""

    def __init__(self, policy=None, stale_after: float | None = None):
        self.policy = policy or (lambda c, r, p: "ok")
        self.stale_after = stale_after
        self.connections = 0
        self.requests: list[tuple[int, int, str]] = []
        self._lock = threading.Lock()
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._accept, daemon=True).start()

    def close(self):
        self.sock.close()

    def _accept(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self.connections += 1
                n = self.connections
            threading.Thread(target=self._serve, args=(conn, n), daemon=True).start()

    def _read_request(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1])
        while len(rest) < length:
            chunk = conn.recv(65536)
            if not chunk:
                return None
            rest += chunk
        return head.split(b"\r\n")[0].split(b" ")[1].decode()

    def _serve(self, conn, n):
        req_no = 0
        last_used = time.monotonic()
        try:
            while True:
                path = self._read_request(conn)
                if path is None:
                    return
                req_no += 1
                idle = time.monotonic() - last_used
                with self._lock:
                    self.requests.append((n, req_no, path))
                action = self.policy(n, req_no, path)
                if self.stale_after is not None and req_no > 1 and idle > self.stale_after:
                    with self._lock:
                        self.stale_hits = getattr(self, "stale_hits", 0) + 1
                    action = "drop"
                last_used = time.monotonic()
                if action == "drop":
                    return                                  # close, no answer
                if action == "hang":
                    time.sleep(30)
                    return
                if isinstance(action, tuple):               # ("slow", seconds)
                    time.sleep(action[1])
                if action == "429":
                    body = json.dumps({"message": "slow down"}).encode()
                    conn.sendall(b"HTTP/1.1 429 Too Many Requests\r\n"
                                 b"Content-Type: application/json\r\n"
                                 b"x-amzn-ErrorType: ThrottlingException\r\nContent-Length: "
                                 + str(len(body)).encode() + b"\r\n\r\n" + body)
                    continue
                if path.endswith("/invoke-with-response-stream"):
                    conn.sendall(b"HTTP/1.1 200 OK\r\n"
                                 b"Content-Type: application/vnd.amazon.eventstream\r\n"
                                 b"x-amzn-RequestId: req-stream\r\nContent-Length: 0\r\n\r\n")
                    continue
                if path.endswith("/count-tokens"):
                    body = json.dumps({"inputTokens": 11}).encode()
                elif path.endswith("/converse"):
                    body = json.dumps({"output": {"message": {"role": "assistant", "content": []}},
                                       "stopReason": "end_turn",
                                       "usage": {"inputTokens": 4, "outputTokens": 1,
                                                 "totalTokens": 5}}).encode()
                else:
                    body = OK
                conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                             b"x-amzn-RequestId: req-real\r\nContent-Length: "
                             + str(len(body)).encode() + b"\r\n\r\n" + body)
        except OSError:
            return
        finally:
            conn.close()


def _client(port, read_timeout=5):
    return boto3.client(
        "bedrock-runtime", region_name="us-west-2", endpoint_url=f"http://127.0.0.1:{port}",
        aws_access_key_id="x", aws_secret_access_key="y",
        # Same retry setting as the gateway's client (main.py): ONE attempt in total, botocore
        # itself never retries. NB ``max_attempts: 1`` would mean "one RETRY" — with it botocore
        # re-sends throttled and timed-out requests on its own and these tests measure botocore.
        config=Config(retries={"total_max_attempts": 1, "mode": "standard"},
                      read_timeout=read_timeout, connect_timeout=5, max_pool_connections=10))


def stale_after_first_use(conn_no, req_no, path):
    """A pooled connection answers its first request, then is dead when it is reused."""
    return "ok" if req_no == 1 else "drop"


@pytest.fixture
def server():
    made: list[FakeBedrock] = []

    def make(policy=None, stale_after=None):
        made.append(FakeBedrock(policy, stale_after))
        return made[-1]

    yield make
    for s in made:
        s.close()


async def test_one_stale_connection_every_call_kind(server):
    srv = server(stale_after_first_use)
    adapter = BedrockAdapter(_client(srv.port))
    assert (await adapter.invoke(b'{"messages":[]}', "m"))[0] == 200    # fills the pool (1 conn)
    # the pooled connection is reused → dead → retried on a fresh one
    status, body, headers, usage = await adapter.invoke(b'{"messages":[]}', "m")
    assert status == 200 and body == OK and usage.input_tokens == 3
    assert headers.get("x-amzn-requestid") == "req-real"
    assert srv.connections == 2, "the dead connection plus one fresh connection"

    status, _b, _h, usage = await adapter.invoke(
        json.dumps({"messages": [{"role": "user", "content": [{"text": "hi"}]}]}).encode(), "m",
        path_suffix="converse")
    assert status == 200 and (usage.input_tokens, usage.output_tokens) == (4, 1)
    assert await adapter.count_tokens(b'{"messages":[]}', "m") == (200, 11)

    status, gen, _h, rid = await adapter.invoke_stream(b'{"messages":[]}', "m")
    assert status == 200 and rid == "req-stream"
    assert [c async for c in gen] == []


@pytest.mark.parametrize("n", [5, 12])
async def test_many_stale_pooled_connections_hit_by_a_burst_after_an_idle_period(server, n):
    srv = server(stale_after=0.25)
    adapter = BedrockAdapter(_client(srv.port))
    first = await asyncio.gather(*(adapter.invoke(b'{"messages":[]}', "m") for _ in range(n)))
    assert [r[0] for r in first] == [200] * n
    assert srv.connections >= 2, "the first burst must leave several connections in the pool"

    await asyncio.sleep(0.4)                                    # … and they all go stale together
    burst = await asyncio.gather(*(adapter.invoke(b'{"messages":[]}', "m") for _ in range(n)))
    assert [r[0] for r in burst] == [200] * n, "every request of the burst must survive"
    assert getattr(srv, "stale_hits", 0) >= 1, "at least one request really hit a dead connection"

    again = await asyncio.gather(*(adapter.invoke(b'{"messages":[]}', "m") for _ in range(n)))
    assert [r[0] for r in again] == [200] * n, "and the client keeps working after the resets"


async def test_an_in_flight_request_survives_a_pool_reset_by_another_request(server):
    def policy(conn_no, req_no, path):
        if conn_no == 1:
            return "ok" if req_no == 1 else "drop"           # the pooled connection: dead on reuse
        if conn_no == 2:
            return ("slow", 1.5)                              # the in-flight request
        return "ok"

    srv = server(policy)
    adapter = BedrockAdapter(_client(srv.port))
    assert (await adapter.invoke(b'{"messages":[]}', "m"))[0] == 200           # conn 1 → pool

    async def slow_then_fast():
        slow = asyncio.create_task(adapter.invoke(b'{"x":"slow"}', "m"))
        await asyncio.sleep(0.05)      # let it take conn 1 … it gets dropped and retries on conn 2
        return slow

    # request A: reuses conn 1 (dead) → resets the pool → retries on conn 2, which answers slowly
    a = await slow_then_fast()
    await asyncio.sleep(0.4)
    # request B arrives while A is in flight on conn 2; whatever it does to the pool, A must finish
    b = await adapter.invoke(b'{"x":"fast"}', "m")
    assert b[0] == 200
    assert (await a)[0] == 200, "a pool reset must not break a request that is already in flight"


async def test_an_error_the_server_answered_with_is_not_retried(server):
    srv = server(lambda c, r, p: "429")
    status, body, _h, _u = await BedrockAdapter(_client(srv.port)).invoke(b'{"messages":[]}', "m")
    assert status == 429 and b"provider_error" in body
    assert len(srv.requests) == 1, "Bedrock replied — a retry could bill twice"


async def test_a_read_timeout_is_not_retried(server):
    srv = server(lambda c, r, p: "hang")
    t0 = time.monotonic()
    status, _b, _h, _u = await BedrockAdapter(_client(srv.port, read_timeout=1)).invoke(
        b'{"messages":[]}', "m")
    assert status == 502
    assert len(srv.requests) == 1, "the request may be running on Bedrock — never send it twice"
    assert time.monotonic() - t0 < 4


async def test_when_every_connection_dies_the_caller_gets_one_502_after_two_attempts(server):
    srv = server(lambda c, r, p: "drop")
    status, body, _h, _u = await BedrockAdapter(_client(srv.port)).invoke(b'{"messages":[]}', "m")
    assert status == 502 and b"provider_error" in body
    assert len(srv.requests) == 2, "exactly one retry — no storm"
