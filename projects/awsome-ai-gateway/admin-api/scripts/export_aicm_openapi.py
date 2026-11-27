# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.
"""/aicm/v1 OpenAPI 명세 추출 — docs/NDS/aicm/aicm-v1-openapi.json 을 다시 만든다.

    cd admin-api && PYTHONPATH=src .venv/bin/python scripts/export_aicm_openapi.py

FastAPI 가 만든 OpenAPI 에서 /aicm/v1 경로와 그 경로가 참조하는 스키마만 남기고, 코드가 헤더에서
직접 읽는 ``Idempotency-Key``(mutation 필수)와 인증 방식을 명시한다.
tests/unit/test_aicm_flag_off.py 가 커밋된 파일과 이 출력이 같은지 검사한다.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parents[2] / "docs" / "NDS" / "aicm" / "aicm-v1-openapi.json"
def _refs(node, out: set[str]) -> None:
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            out.add(ref.rsplit("/", 1)[1])
        for v in node.values():
            _refs(v, out)
    elif isinstance(node, list):
        for v in node:
            _refs(v, out)


def build_spec(full: dict) -> dict:
    paths = {p: v for p, v in full["paths"].items() if p.startswith("/aicm/v1")}
    schemas = full.get("components", {}).get("schemas", {})
    needed: set[str] = set()
    _refs(paths, needed)
    while True:
        more: set[str] = set()
        for name in needed:
            _refs(schemas.get(name, {}), more)
        if more <= needed:
            break
        needed |= more
    for path, ops in paths.items():
        for method, op in ops.items():
            op["security"] = [{"integrationTokenSvc": []}]
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "LLM Gateway — AICM integration API",
            "version": "1.0.0",
            "description": (
                "admin-api /aicm/v1 라우터(AICM_API_ENABLED=true 일 때만 존재). "
                "/aicm/v1/admin mirror: Authorization: Bearer svc-<64 hex>(AICM scoped). "
                "오류 봉투: {\"error\": {\"type\", \"code\", \"message\", \"details\"?}}. "
                "안내서: docs/NDS/aicm/README.md"
            ),
        },
        "paths": paths,
        "components": {
            "schemas": {k: schemas[k] for k in sorted(needed)},
            "securitySchemes": {
                "integrationTokenSvc": {"type": "http", "scheme": "bearer", "bearerFormat": "svc-<64 hex>, AICM scoped"},
            },
        },
    }


def render(spec: dict) -> str:
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    os.environ["AICM_API_ENABLED"] = "true"
    os.environ.setdefault("VIRTUAL_KEY_ENCRYPTION_KEY", "0" * 64)
    from app.main import create_app

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(render(build_spec(create_app().openapi())), encoding="utf-8")
    print(f"wrote {OUT}", file=sys.stderr)


if __name__ == "__main__":
    main()
