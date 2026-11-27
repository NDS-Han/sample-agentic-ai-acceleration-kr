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
_HUMAN_ONLY = {("post", "/aicm/v1/service-tokens")}
# Idempotency-Key 는 통합 mutation 의 계약 — admin mirror(/aicm/v1/admin/*)는 /admin 과 같은
# 인증 모델이라 대상이 아니고, service-tokens 발급(사람 전용)도 멱등 프레임워크를 쓰지 않는다.
def _is_idempotent_mutation(method: str, path: str) -> bool:
    if method not in ("post", "put", "delete"):
        return False
    if path.startswith("/aicm/v1/admin/"):
        return False
    return (method, path) != ("post", "/aicm/v1/service-tokens")


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
            if (method, path) in _HUMAN_ONLY:
                op["security"] = [{"adminJwt": []}]
                continue
            op["security"] = [
                {"integrationTokenSvc": []} if path.startswith("/aicm/v1/admin/")
                else {"integrationTokenLegacy": []}
            ]
            if _is_idempotent_mutation(method, path):
                op.setdefault("parameters", []).append({
                    "name": "Idempotency-Key", "in": "header", "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 128},
                    "description": "같은 키+같은 요청 → 저장된 응답 재생(Idempotent-Replayed: true). "
                                   "다른 요청 → 422 idempotency_key_reused. 보존 7일.",
                })
    return {
        "openapi": full["openapi"],
        "info": {
            "title": "LLM Gateway — AICM integration API",
            "version": "1.0.0",
            "description": (
                "admin-api /aicm/v1 라우터(AICM_API_ENABLED=true 일 때만 존재). "
                "/aicm/v1/admin mirror: Authorization: Bearer svc-<64 hex>(AICM scoped). "
                "legacy lifecycle routes: Bearer itg-<64 hex>. "
                "오류 봉투: {\"error\": {\"type\", \"code\", \"message\", \"details\"?}}. "
                "안내서: docs/NDS/aicm/README.md"
            ),
        },
        "paths": paths,
        "components": {
            "schemas": {k: schemas[k] for k in sorted(needed)},
            "securitySchemes": {
                "integrationTokenSvc": {"type": "http", "scheme": "bearer", "bearerFormat": "svc-<64 hex>, AICM scoped"},
                "integrationTokenLegacy": {"type": "http", "scheme": "bearer", "bearerFormat": "itg-<64 hex>"},
                "adminJwt": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT (human admin)"},
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
