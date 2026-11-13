# syntax=docker/dockerfile:1.7
# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# AgentCore Runtime 컨테이너 contract:
# - 8080 포트에 /invocations + /ping HTTP endpoint 노출 (BedrockAgentCoreApp 자동 처리)
# - non-root user 권장
# - linux/arm64 필수 — AgentCore Runtime 은 Graviton(arm64) microVM 에서만 실행.
#   amd64 image 를 push 하면 CreateAgentRuntime 이 ValidationException
#   ("Architecture incompatible ... Supported platforms: [arm64]") 반환.
#   빌드: finch build --platform linux/arm64 ...

FROM public.ecr.aws/docker/library/python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 최소 system 패키지 — strands 가 grpc/protobuf 빌드에 필요할 수 있음
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        build-essential \
 && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.13.7 /uv /usr/local/bin/uv

WORKDIR /app

# uv.lock* 가 있으면 사용, 없어도 동작 (glob)
COPY pyproject.toml uv.lock* ./
COPY src ./src

# admin-api 와 동일 규약: lock 을 requirements 로 변환해 설치하고,
# 프로젝트는 pip install 이 아니라 PYTHONPATH 로 — `pip install -e .` 는
# pyproject 의 >= 스펙을 빌드마다 재해석해 lock 을 무시한다 (R36).
RUN if [ -f uv.lock ]; then \
      uv export --frozen --no-dev --format requirements-txt -o /tmp/requirements.txt; \
    else \
      cp pyproject.toml /tmp/requirements.txt; \
    fi \
 && uv pip install --system --no-cache -r /tmp/requirements.txt

ENV PYTHONPATH=/app/src

# AgentCore HTTP contract
EXPOSE 8080

# non-root
RUN useradd -m -u 1000 agent && chown -R agent:agent /app
USER agent

CMD ["python", "-m", "agent.main"]
