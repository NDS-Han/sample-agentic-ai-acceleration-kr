"""AWS event-stream(vnd.amazon.eventstream) 바이너리 프레임 인코더.

botocore 는 디코더(EventStreamBuffer)만 제공하고 인코더는 없어서 직접 만든다.
실제 Bedrock `invoke_model_with_response_stream` 응답과 같은 바이트 레이아웃:

    | total_len u32 | headers_len u32 | prelude_crc u32 |
    | headers ...   | payload ...     | message_crc u32 |

prelude_crc = crc32(앞 8B), message_crc = crc32(마지막 4B 이전 전체).
헤더 하나 = name_len u8 + name + value_type u8 + value
  - value_type 7 (string) = len u16 + utf8
  - value_type 0/1 (bool true/false) = 값 없음

실제 Bedrock 프레임 형태(서비스 모델 + 파서 동작으로 검증):
  - chunk 이벤트: `:message-type: event`, `:event-type: chunk`,
    `:content-type: application/json`, payload = {"bytes": "<base64 모델바이트>"}.
    PayloadPart.bytes 는 eventpayload 마커 없이 JSON body 로 파싱된다 —
    base64 를 넣지 않으면 어댑터가 chunk.bytes 를 못 얻는다.
  - 예외 프레임: `:message-type: exception`, `:exception-type: <멤버명>`,
    payload = {"message": "..."} → botocore 가 EventStreamError 로 raise.
"""

from __future__ import annotations

import base64
import json
import struct
from zlib import crc32

_HEADER_STRING = 7
_HEADER_BOOL_TRUE = 0
_HEADER_BOOL_FALSE = 1


def _encode_header(name: str, value: str | bool) -> bytes:
    """단일 헤더 직렬화 — name_len|name|type|(len|value)."""
    name_b = name.encode("utf-8")
    out = struct.pack(">B", len(name_b)) + name_b
    if isinstance(value, bool):
        out += struct.pack(">B", _HEADER_BOOL_TRUE if value else _HEADER_BOOL_FALSE)
        return out
    val_b = value.encode("utf-8")
    return out + struct.pack(">BH", _HEADER_STRING, len(val_b)) + val_b


def encode_message(payload: bytes, headers: dict[str, str | bool]) -> bytes:
    """eventstream 프레임 하나를 직렬화한다."""
    hdr = b"".join(_encode_header(k, v) for k, v in headers.items())
    total_len = 8 + 4 + len(hdr) + len(payload) + 4  # prelude+crc + body + crc
    prelude = struct.pack(">II", total_len, len(hdr))
    prelude_crc = struct.pack(">I", crc32(prelude))
    body = hdr + payload
    message_crc = struct.pack(">I", crc32(prelude + prelude_crc + body))
    return prelude + prelude_crc + body + message_crc


def chunk_event(anthropic_event: dict) -> bytes:
    """Anthropic-wire 이벤트 dict → `chunk` 프레임.

    실제 Bedrock 처럼 payload 를 {"bytes": base64(모델바이트)} 로 감싼다 —
    파싱되면 event["chunk"]["bytes"] 에 모델바이트가 들어가고 어댑터가
    그것을 그대로 클라이언트에 재송신한다.
    """
    payload = json.dumps(
        {"bytes": base64.b64encode(
            json.dumps(anthropic_event).encode("utf-8")).decode("ascii")}
    ).encode("utf-8")
    return encode_message(payload, {
        ":message-type": "event",
        ":event-type": "chunk",
        ":content-type": "application/json",
    })


def converse_event(member: str, event_body: dict) -> bytes:
    """converse-stream 유니온 이벤트 — payload 는 이벤트 자체의 JSON."""
    return encode_message(json.dumps(event_body).encode("utf-8"), {
        ":message-type": "event",
        ":event-type": member,
        ":content-type": "application/json",
    })


def exception_event(exc_name: str, message: str) -> bytes:
    """미드스트림 예외 프레임 — botocore 가 EventStreamError 로 raise."""
    return encode_message(json.dumps({"message": message}).encode("utf-8"), {
        ":message-type": "exception",
        ":exception-type": exc_name,
        ":content-type": "application/json",
    })
