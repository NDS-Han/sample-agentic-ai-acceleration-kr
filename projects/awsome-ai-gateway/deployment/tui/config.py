# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""폼 입력 ↔ terraform.tfvars 직렬화/파싱 + 플레이스홀더 검증. 순수 함수만."""
from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

PLACEHOLDER_TOKENS: tuple[str, ...] = (
    "CHANGE_ME",
    "CHANGE_ACCOUNT_ID",
    "ACCOUNT_ID",
    "YOUR_ROLE",
    "tvly-...",
    "BSA...",
    "sk-...",
)


def find_placeholders(values: dict[str, str]) -> list[str]:
    """값에 플레이스홀더 토큰이 남은 key 목록을 반환."""
    flagged = []
    for key, val in values.items():
        if isinstance(val, str) and any(tok in val for tok in PLACEHOLDER_TOKENS):
            flagged.append(key)
    return flagged


def _hcl_value(val, indent: int = 0) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (int, float)):
        return str(val)
    if isinstance(val, list):
        return "[" + ", ".join(_hcl_value(v, indent) for v in val) + "]"
    if isinstance(val, dict):
        # HCL object/map: 중첩 블록(eks_access_entries, tags 등)을 들여쓰기해 직렬화.
        # key는 bare identifier로 씀(tfvars object 문법).
        pad = "  " * (indent + 1)
        close = "  " * indent
        inner = "\n".join(
            f"{pad}{k} = {_hcl_value(v, indent + 1)}" for k, v in val.items()
        )
        return "{\n" + inner + "\n" + close + "}"
    return f'"{val}"'


def to_tfvars(values: dict) -> str:
    """dict를 HCL tfvars 문자열로 직렬화. dict 값은 중첩 블록으로 나감."""
    return "\n".join(f"{k} = {_hcl_value(v)}" for k, v in values.items()) + "\n"


_TFVARS_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=")


def merge_tfvars(existing: str, values: dict) -> str:
    """기존 tfvars 원문을 보존하면서 ``values`` 의 키만 갱신한다.

    통째 덮어쓰기(``to_tfvars`` 직렬화)는 parse 가 못 읽는 내용을 전부
    버린다 — 주석, 손으로 추가한 변수, 그리고 ``parse_tfvars`` 가 의도적으로
    건너뛰는 중첩 블록(eks_access_entries·tags·azs 같은 멀티라인 값)이
    TUI 실행 때마다 소실됐다.

    규칙: 최상위 ``key = ...`` 정의가 ``values`` 에 있으면 새 직렬화로 교체
    (블록 값이면 닫히는 ``}`` 까지 통째로 교체), 없으면 원문 그대로 보존.
    파일에 없던 키는 끝에 추가한다.
    """
    lines = existing.splitlines()
    out: list[str] = []
    seen: set[str] = set()
    i, depth = 0, 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        at_top = depth == 0
        if stripped.startswith("#") or stripped.startswith("//"):
            # 주석 안의 브레이스는 depth 를 오염시킨다 — 그대로 보존하고 건너뛴다.
            out.append(line)
            i += 1
            continue
        opens, closes = stripped.count("{"), stripped.count("}")
        m = _TFVARS_KEY_RE.match(line) if at_top else None
        if m and m.group(1) in values:
            key = m.group(1)
            seen.add(key)
            out.append(f"{key} = {_hcl_value(values[key])}")
            # 블록/멀티라인 값 전체를 건너뛴다 — `{`(map/block)와 `[`(list) 둘 다
            #   값이 여러 줄일 수 있다. 안의 줄을 보존하면 고아 파편으로 문법이 깨진다.
            rest = opens - closes + stripped.count("[") - stripped.count("]")
            if rest > 0:
                depth += rest
                i += 1
                while i < len(lines) and depth > 0:
                    s = lines[i]
                    depth += (s.count("{") - s.count("}")
                              + s.count("[") - s.count("]"))
                    i += 1
                continue
            i += 1
            continue
        depth += opens - closes + stripped.count("[") - stripped.count("]")
        out.append(line)
        i += 1
    for k, v in values.items():
        if k not in seen:
            out.append(f"{k} = {_hcl_value(v)}")
    return "\n".join(out) + "\n"


def write_tfvars(path: Path, values: dict) -> None:
    """merge-write — 기존 파일의 모르는 내용을 보존한다(``merge_tfvars`` 참조)."""
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    path.write_text(merge_tfvars(existing, values), encoding="utf-8")


def to_key_file(keys: dict[str, str]) -> str:
    """{engine: api_key} → seed-tool-secrets.sh 형식(`engine=value` 줄) 직렬화.

    API 키는 절대 terraform(tfvars/tfstate)을 거치면 안 되고, 이 파일 형식으로
    provision_tool_gateway.sh 의 TOOL_KEY_FILE 에 넘겨 Secrets Manager 에 직접
    주입한다. 빈 값 엔진은 건너뛴다."""
    return "".join(f"{engine}={key}\n" for engine, key in keys.items() if key)


def write_key_file(keys: dict[str, str]) -> Path:
    """키를 0600 권한 임시 파일에 기록하고 경로를 반환. 값이 하나도 없으면 만들지 않는다.

    tfvars 처럼 리포 안에 두지 않는다 — seed 후 호출측이 지운다."""
    body = to_key_file(keys)
    if not body:
        return None
    fd, name = tempfile.mkstemp(prefix="tool-keys-", suffix=".env")
    try:
        os.write(fd, body.encode())
    finally:
        os.close(fd)
    os.chmod(name, 0o600)
    return Path(name)


def parse_tfvars(text: str) -> dict:
    """기존 tfvars에서 최상위 `key = value` scalar만 파싱(프리필용).

    중첩 블록({...}) 내부 줄은 건너뛴다 — 안 그러면 eks_access_entries/tags 안의
    principal_arn·CostCenter 등이 최상위 키로 잘못 승격돼, 재직렬화 시 블록 구조가
    깨지고 'undeclared variable' 경고 + access entry 소실로 이어진다.
    문자열/bool/숫자 scalar만 반환하고 list/dict(멀티라인) 값은 프리필 대상이 아니다.
    """
    result: dict = {}
    depth = 0
    for line in text.splitlines():
        stripped = line.strip()
        opens, closes = stripped.count("{"), stripped.count("}")
        at_top = depth == 0
        depth += opens - closes
        if not at_top:
            continue  # 중첩 블록 내부 — 승격 금지
        if opens > closes:
            continue  # 이 줄이 블록을 열었음 (예: `tags = {`)
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        key, raw = key.strip(), raw.strip()
        if raw in ("true", "false"):
            result[key] = raw == "true"
        elif raw.startswith('"') and raw.endswith('"'):
            result[key] = raw[1:-1]
        elif raw.lstrip("-").isdigit():
            result[key] = int(raw)
        # list/multiline 값은 프리필 대상 아님 — 무시
    return result


def prefill_scalar(text: str, key: str) -> str:
    """tfvars 원문에서 `key = "..."` scalar를 깊이 무관하게 첫 매치로 추출(프리필 default용).

    parse_tfvars는 중첩 블록을 건너뛰므로, 블록 안에 있는 principal_arn 같은 값을
    재입력 없이 프리필하려면 별도로 스캔해야 한다."""
    for line in text.splitlines():
        s = line.strip()
        k, sep, raw = s.partition("=")
        if not sep or k.strip() != key:
            continue
        raw = raw.strip()
        if raw.startswith('"') and raw.endswith('"'):
            return raw[1:-1]
    return ""


@dataclass
class BackendConfig:
    """tfstate 백엔드 — bootstrap과 tf-init에 동일 값 주입하는 단일 소스.

    region이 지정되면 tfstate 버킷 region도 -backend-config로 덮어쓴다. 빈 값이면
    backend.tf에 하드코딩된 region을 그대로 쓴다(하위호환)."""
    bucket: str
    dynamodb_table: str
    region: str = ""

    def backend_args(self) -> list[str]:
        args = [
            f"-backend-config=bucket={self.bucket}",
            f"-backend-config=dynamodb_table={self.dynamodb_table}",
        ]
        if self.region:
            args.append(f"-backend-config=region={self.region}")
        return args
