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


def _dmap(v) -> dict:
    """값이 dict 면 그대로, 아니면 {} — 사람 소유 YAML 의 스칼라/리스트 잎이
    .get 연쇄를 AttributeError 로 죽이는 걸 방어한다."""
    return v if isinstance(v, dict) else {}


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
            # <<-TAG 는 종결자 들여쓰기를 허용하고 <<- 는 0컬럼을 요구한다 —
            # 들여쓴 종결자를 못 찾으면 heredoc 이후 전부가 지워져 그 뒤의
            # environment= 같은 선언이 보이지 않게 된다. <<"TAG"/<<-"TAG"
            # (인용 구분자 — interpolation 비활성)도 같은 규칙으로 인식한다 —
            # 놓치면 본문의 key=value 가 유령 선언으로 살아난다.
            m = re.match(r"<<(-?)(?:[\"'](\w[\w-]*)[\"']|(\w[\w-]*))", text[i:])
            if m:
                tag = re.escape(m.group(2) or m.group(3))
                pat = (rf"(?m)^\s*{tag}\s*$" if m.group(1)
                       else rf"(?m)^{tag}\s*$")
                end = re.search(pat, text[i:])
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


# 도구가 쓰는 관리 키 — yaml 선언이 빠지면 .env 에서도 수렴(삭제)해야 한다.
# stale OIDC_* 가 남으면 admin-api 가 dev 토큰을 거부해 dev-login 이 401 로
# 잠기고, stale WEB_SEARCH/AGENTCORE_* 는 꺼진 기능이 계속 켜진다.
# GENERATED_KEYS(시크릿)는 절대 여기 넣지 않는다 — 삭제되면 발급 키가 전부
# 무효화된다.
# AGENTCORE_* 는 apply 가 프로비저닝 결과로 주입하는 키라 desired_env 에
# 나타나지 않는다 — web_search 기능 자체가 꺼질 때(WEB_SEARCH_ENABLED 드롭)
# 만 함께 지운다.
# 접두사 그룹은 앵커 키(desired_env 가 기능을 켤 때만 방출하는 대표 키)가
# 사라질 때만 통째로 수렴한다 — desired_env 가 한 번도 방출하지 않는
# 운영자 확장 키(SMTP_USERNAME·OIDC_EMAIL_CLAIM 등)를 기능이 켜진 채로
# 지우는 사고를 막는다.
_MANAGED_PREFIX_ANCHORS = {"OIDC_": "OIDC_ISSUER_URL", "SMTP_": "SMTP_HOST"}
_MANAGED_KEYS = frozenset({
    "POSTGRES_USER", "POSTGRES_DB", "AWS_REGION", "AWS_DEFAULT_REGION",
    "ALLOWED_STS_REGIONS", "ALLOWED_AWS_ACCOUNTS", "ALLOWED_IAM_ROLES",
    "DEV_LOGIN_ENABLED", "EMAIL_SENDER_TYPE", "EMAIL_SENDER_ADDRESS",
    "LOG_LEVEL", "SERVICE_KEYS_ENABLED", "WEB_SEARCH_ENABLED",
    "AWS_SES_REGION", "EMAIL_API_URL", "ADMIN_GROUPS", "ADMIN_EMAILS",
})


def _is_managed(key: str, desired: dict[str, str]) -> bool:
    if key in _MANAGED_KEYS:
        return True
    for prefix, anchor in _MANAGED_PREFIX_ANCHORS.items():
        if key.startswith(prefix) and anchor not in desired:
            return True
    return False


def merge_env(existing: dict[str, str],
              desired: dict[str, str]) -> tuple[dict[str, str], list[str], list[str]]:
    """existing 을 보존하고 desired 의 누락 키만 채운다.

    desired 에 있는 값이 existing 과 다르면 **덮지 않고** 이름을 돌려준다 —
    운영자가 의도를 가지고 바꾼 값일 수 있으므로 결정권을 사람에게 둔다.
    도구 관리 키가 existing 에 있는데 desired 에 없으면(선언 해제) 삭제해
    declared-off 수렴을 맞춘다 — 운영자가 손으로 넣은 비관리 키는 보존한다.
    반환: (merged, preserved_differing_keys, dropped_managed_keys)
    """
    merged = dict(existing)
    differing: list[str] = []
    for k, v in desired.items():
        if k not in merged or merged[k] == "":
            merged[k] = v
        elif merged[k] != v:
            differing.append(k)
    dropped = [k for k in list(merged)
               if k not in desired and k not in GENERATED_KEYS
               and _is_managed(k, desired)]
    if "WEB_SEARCH_ENABLED" in dropped:
        # 기능이 꺼지면 apply 주입 URL/target 도 함께 수렴 — 남겨두면
        # 설정 파일엔 없는 외부 엔드포인트를 앱이 계속 가리킨다.
        dropped += [k for k in list(merged)
                    if k.startswith("AGENTCORE_") and k not in dropped]
    for k in dropped:
        del merged[k]
    return merged, differing, dropped


def fill_generated(env: dict[str, str]) -> list[str]:
    """GENERATED_KEYS 중 비어 있는 키를 생성해 채운다. 채운 키 목록 반환."""
    filled: list[str] = []
    for k, fn in GENERATED_KEYS.items():
        if not env.get(k):
            env[k] = fn()
            filled.append(k)
    return filled


def write_env_file(path: Path, env: dict[str, str], header: str) -> None:
    for k, v in env.items():
        # KEY=VALUE 를 raw 로 쓰는 형식 — 개행이 섞이면 다음 줄이 별도 키로
        # 파싱돼 임의 env 주입/파일 파괴가 된다. yaml block-scalar 등에서
        # 들어오는 개행은 선언 단계에서 걸러지지 않으므로 여기서 차단한다.
        if "\n" in str(v) or "\r" in str(v):
            raise SystemExit(f".env 키 {k} 의 값에 개행이 있습니다 — "
                             "gateway.yaml 선언을 확인하세요")
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
