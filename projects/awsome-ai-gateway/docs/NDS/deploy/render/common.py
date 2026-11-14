# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""렌더 공통 유틸 — 시크릿 생성, .env 병합(기존 값 보존), 산출물 쓰기.

핵심 불변식: 재렌더해도 이미 발급된 시크릿은 절대 바뀌지 않는다. 비밀번호·DEK 가
바뀌면 VK 전원 무효화·세션 파괴가 일어나므로, .env 는 "없는 키만 채우는" 방식.
"""
from __future__ import annotations

import os
import re
import secrets
from pathlib import Path


def tfvars_clean(text: str) -> str:
    """tfvars 텍스트에서 주석·heredoc 본문·{} 맵 본문을 제거한 depth-0 잔여분.

    단일 패스 스캐너 — regex 만으로는 "문자열 안의 # / 들여쓰기 없는 map 본문
    안의 key=" 처럼 상태가 필요한 경우를 못 걸러 오파싱한다:
      - ``#``, ``//`` 라인 주석과 ``/* */`` 블록 주석 (문자열 밖일 때만)
      - ``<<TAG``/``<<-TAG`` heredoc — 종결 ``^TAG$`` 까지 통째로 제거
      - ``{ ... }`` 본문 — map 값 안의 key=val 은 최상위 할당이 아니다
    문자열 내용("a#b", "a]b")은 보존된다 — 스캐너가 문자열 상태를 추적하므로.
    """
    buf: list[str] = []
    i, n, depth = 0, len(text), 0
    in_str = False
    while i < n:
        c = text[i]
        if in_str:
            # 문자열 상태는 추적해 "} 안의 }" 가 depth 를 깎지 않게 한다 —
            # 다만 map 본문(depth>0) 안의 문자열은 버퍼에 넣지 않는다
            if depth == 0:
                buf.append(c)
                if c == "\\" and i + 1 < n:      # 이스케이프 쌍은 통째로 통과
                    buf.append(text[i + 1])
                    i += 2
                    continue
            elif c == "\\" and i + 1 < n:
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            if depth == 0:
                buf.append(c)
            i += 1
            continue
        if c == "#" or text[i:i + 2] == "//":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if text[i:i + 2] == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        if text[i:i + 2] == "<<":
            m = re.match(r"<<-?(\w+)", text[i:])
            if m:
                end = re.search(rf"(?m)^{re.escape(m.group(1))}\s*$", text[i:])
                i = n if not end else i + end.end()
                continue
        if c == "{":
            depth += 1
            i += 1
            continue
        if c == "}":
            depth = max(0, depth - 1)
            i += 1
            continue
        if depth == 0:
            buf.append(c)
        i += 1
    return "".join(buf)


def gen_password() -> str:
    return secrets.token_urlsafe(24)


def gen_hex32() -> str:
    """AES-256-GCM DEK 용 64-hex."""
    return secrets.token_hex(32)


def gen_secret() -> str:
    return secrets.token_urlsafe(32)


# .env 에 자동 생성할 수 있는 키 → 생성 함수. 이 표에 없는 값(도메인·OIDC 등
# 외부에서 정해야 하는 값)은 절대 자동 생성하지 않고 placeholder 만 남긴다.
GENERATED_KEYS = {
    "POSTGRES_PASSWORD": gen_password,
    "NEXTAUTH_SECRET": gen_secret,
    "INTERNAL_API_TOKEN": gen_secret,
    "VIRTUAL_KEY_ENCRYPTION_KEY": gen_hex32,
    # observability=true 일 때 grafana admin 비번 — 없으면 compose 디폴트 "admin".
    "GF_SECURITY_ADMIN_PASSWORD": gen_password,
}


def parse_env_file(path: Path) -> dict[str, str]:
    """기존 .env 를 key=value dict 로. 주석·빈줄·export 접두사 무시."""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip().removeprefix("export ").strip()] = v.strip()
    return out


def merge_env(existing: dict[str, str], desired: dict[str, str]) -> tuple[dict[str, str], list[str]]:
    """existing 을 보존하고 desired 의 누락 키만 채운다.

    desired 에 있는 값이 existing 과 다르면 **덮지 않고** 이름을 돌려준다 —
    운영자가 의도를 가지고 바꾼 값일 수 있으므로 결정권을 사람에게 둔다.
    반환: (merged, preserved_differing_keys)
    """
    merged = dict(existing)
    differing: list[str] = []
    for k, v in desired.items():
        if k not in merged or merged[k] == "":
            merged[k] = v
        elif merged[k] != v:
            differing.append(k)
    return merged, differing


def fill_generated(env: dict[str, str]) -> list[str]:
    """GENERATED_KEYS 중 비어 있는 키를 생성해 채운다. 채운 키 목록 반환."""
    filled: list[str] = []
    for k, fn in GENERATED_KEYS.items():
        if not env.get(k):
            env[k] = fn()
            filled.append(k)
    return filled


def write_env_file(path: Path, env: dict[str, str], header: str) -> None:
    lines = [header.rstrip(), ""]
    for k, v in env.items():
        lines.append(f"{k}={v}")
    # write_text()+chmod 는 시크릿이 잠깐 umask 권한(0644)으로 노출되는
    # TOCTOU 창이 있다. 처음부터 0600 으로 생성한다.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    # os.open 의 mode 는 신규 생성에만 적용 — 기존 파일(0644 로 남은 재렌더,
    # cp 로 가져온 백업)은 O_TRUNC 로 내용만 덮어 퍼미션이 그대로다. 무조건
    # fchmod 해서 시크릿 파일이 0600 아래에 있음을 보장한다.
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
