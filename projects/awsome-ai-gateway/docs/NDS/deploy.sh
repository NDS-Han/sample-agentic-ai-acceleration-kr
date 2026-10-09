#!/usr/bin/env bash
# LLM Gateway 배포 도구 — gateway.yaml 기반
# 사용: ./deploy.sh init|validate|render|apply|doctor [--config deployment/gateway.yaml]
# 프로젝트 루트의 ./deploy 가 이 스크립트로 전달한다.
set -euo pipefail
cd "$(dirname "$0")/../.."   # docs/NDS → 프로젝트 루트 (awsome-ai-gateway)
PY="docs/NDS/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
PYTHONPATH="docs/NDS${PYTHONPATH:+:$PYTHONPATH}" exec "$PY" -m deploy "$@"
