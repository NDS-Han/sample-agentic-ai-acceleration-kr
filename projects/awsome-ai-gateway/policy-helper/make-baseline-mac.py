#!/usr/bin/env python3
"""지금 이 Mac 에 적용 중인 관리형 설정을 그대로 옮겨 policy-helper 의 기준값(baseline.json)을 만든다.

Claude Code 가 하는 대로 `managed-settings.json` 에 `managed-settings.d/*.json` 을 이름순으로 합치고
(단일 값은 뒤 파일이 덮음, 목록은 합집합, 중첩 블록은 키별 병합, `extraKnownMarketplaces`·
`managedMcpServers` 항목은 이름 단위로 통째 교체), 설정이 아닌 `_` 키와 `policyHelper` 를 빼고,
잠금 네 가지의 엄격한 기본값(없을 때만)을 더한다. 결과는 ~/ph-build/baseline.json (0600).
실행: python3 make-baseline-mac.py [--dir <managed dir>] [--out <file>]

Build policy-helper's baseline.json from the managed settings this Mac applies today.
Merges managed-settings.json + managed-settings.d/*.json the way Claude Code does,
drops `_`-prefixed keys and `policyHelper`, adds the four strict lock defaults when absent.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

DEFAULT_DIR = "/Library/Application Support/ClaudeCode"
DEFAULT_OUT = Path.home() / "ph-build" / "baseline.json"
# 뒤 파일의 항목이 이름 단위로 통째 교체되는 키 / keys whose named entries replace whole
ENTRY_KEYS = {"extraKnownMarketplaces", "managedMcpServers"}
# 뒤 파일의 값이 통째로 교체되는 키 / keys whose value replaces whole
WHOLE_KEYS = {"fallbackModel", "modelPicker"}
# 허브도 캐시도 없을 때 PC 가 떨어지는 엄격한 기본값 / strict defaults when no server policy
STRICT_DEFAULTS = {
    "disableSideloadFlags": True,
    "strictPluginOnlyCustomization": True,
    "allowManagedMcpServersOnly": True,
    "strictKnownMarketplaces": [],
}


def merge_docs(base: dict, over: dict, top: bool = True) -> dict:
    """두 관리형 문서를 Claude Code 의 드롭인 규칙으로 합친다.

    단일 값은 `over` 가 덮고, 목록은 중복 없이 이어 붙이며, 중첩 블록은 키별로 같은 규칙을
    다시 적용한다. 최상위의 ENTRY_KEYS 는 항목 이름 단위로, WHOLE_KEYS 는 값 통째로 교체된다.

    Merge two managed documents with Claude Code's drop-in rules: scalars replace, lists
    union, nested blocks merge key by key; top-level ENTRY_KEYS replace per named entry,
    WHOLE_KEYS replace whole.
    """
    out = dict(base)
    for key, value in over.items():
        if key not in out or (top and key in WHOLE_KEYS):
            out[key] = value
        elif top and key in ENTRY_KEYS and isinstance(value, dict) and isinstance(out[key], dict):
            out[key] = {**out[key], **value}
        elif isinstance(value, dict) and isinstance(out[key], dict):
            out[key] = merge_docs(out[key], value, top=False)
        elif isinstance(value, list) and isinstance(out[key], list):
            out[key] = out[key] + [v for v in value if v not in out[key]]
        else:
            out[key] = value
    return out


def load_managed(directory: Path) -> tuple[dict, list[str]]:
    """관리형 폴더의 문서를 Claude Code 순서(주 파일 → 드롭인 이름순)로 읽어 합친다.

    Read and merge the managed documents in Claude Code's order: the main file, then
    `managed-settings.d/*.json` sorted by name. Returns the merge and the files used.
    """
    files: list[Path] = []
    main = directory / "managed-settings.json"
    if main.is_file():
        files.append(main)
    dropins = directory / "managed-settings.d"
    if dropins.is_dir():
        files += sorted(p for p in dropins.iterdir() if p.suffix == ".json" and not p.name.startswith("."))
    merged: dict = {}
    for path in files:
        doc = json.loads(path.read_text(encoding="utf-8-sig") or "{}")
        if not isinstance(doc, dict):
            sys.exit(f"{path}: top level is not a JSON object")
        merged = merge_docs(merged, doc)
    return merged, [str(p) for p in files]


def to_baseline(managed: dict) -> tuple[dict, list[str]]:
    """합친 관리형 문서를 기준값으로 다듬는다: `_` 키·policyHelper 제거, 엄격한 잠금 기본값 보충.

    Turn the merged managed document into the baseline: drop `_`-prefixed keys and
    `policyHelper`, add STRICT_DEFAULTS for keys that are absent. Returns it with notes.
    """
    notes: list[str] = []
    baseline = {k: v for k, v in managed.items() if not k.startswith("_") and k != "policyHelper"}
    dropped = sorted(set(managed) - set(baseline))
    if dropped:
        notes.append(f"dropped non-baseline keys: {dropped}")
    for key, value in STRICT_DEFAULTS.items():
        if key not in baseline:
            baseline[key] = value
            notes.append(f"added strict default {key} = {json.dumps(value)}")
        else:
            notes.append(f"kept existing {key} = {json.dumps(baseline[key])}")
    env = baseline.get("env") or {}
    if not isinstance(env, dict) or not env.get("ANTHROPIC_BASE_URL"):
        notes.append("WARNING: env.ANTHROPIC_BASE_URL missing — the helper cannot fetch a server policy")
    for name, value in (("apiKeyHelper", baseline.get("apiKeyHelper")),
                        ("statusLine.command", (baseline.get("statusLine") or {}).get("command"))):
        if isinstance(value, str) and value and not value.strip('"').startswith("/"):
            notes.append(f"WARNING: {name} is not an absolute path ({value!r}); GUI-launched sessions may not find it")
    return baseline, notes


def main() -> None:
    """명령줄 진입점: 읽기 → 다듬기 → ~/ph-build/baseline.json 에 0600 으로 쓰고 화면에 보여 준다.

    CLI entry: load, shape, write ~/ph-build/baseline.json with mode 0600, print it.
    """
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=DEFAULT_DIR, help="managed settings directory")
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="where to write baseline.json")
    args = ap.parse_args()

    managed, files = load_managed(Path(args.dir))
    if not files:
        sys.exit(f"no managed settings found under {args.dir}")
    baseline, notes = to_baseline(managed)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(out, 0o600)

    print("merged from:")
    for f in files:
        print("  ", f)
    for n in notes:
        print("note:", n)
    print(f"\nwrote {out}  (review it, then: bash build.sh {out})\n")
    print(out.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
