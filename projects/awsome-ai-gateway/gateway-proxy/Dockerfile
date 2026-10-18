# Copyright 2026 © Amazon.com and Affiliates: This deliverable is considered Developed Content as defined in the AWS Service Terms.

# Build stage
FROM public.ecr.aws/docker/library/python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app

COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

COPY pyproject.toml uv.lock* ./
RUN uv sync --frozen --no-dev 2>/dev/null || uv sync --no-dev

# Runtime stage
FROM public.ecr.aws/docker/library/python:3.11-slim

RUN groupadd -g 1000 appuser && useradd -u 1000 -g appuser appuser

WORKDIR /app

COPY --from=builder /app/.venv /app/.venv
COPY src/ ./src/
COPY ca-certificates.cr[t] /etc/ssl/certs/ca-certificates.crt

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app/src"
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
ENV REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-certificates.crt

USER appuser

EXPOSE 8000

# ⚠️ 워커 수는 WORKERS env 로 제어(chart values 에서 환경별 조정). exec-form
# CMD ["...","--workers","4"] 는 env 확장이 안 돼 chart 의 WORKERS 설정이 죽은
# 설정이 됐었음 → shell-form + ${WORKERS:-4} 로 살림(미설정 시 기본 4 = 무회귀).
# config.Settings.uvicorn_workers 도 같은 우선순위(WORKERS→UVICORN_WORKERS→4)를
# 쓰므로 레거시 env 만 잡힌 배포에서도 실제 워커 수와 rate-limit divisor 가
# 어긋나지 않게 둘 다 본다(R16-V5).
# Fargate 0.5 vCPU 에서 4워커 = 워커당 0.125 vCPU CPU 기아 → values 에서 1~2 권장.
# ⚠️ --timeout-graceful-shutdown 필수: SIGTERM 시 uvicorn 은 열린 SSE 연결이 끝날
#   때까지 무제한 대기하고 lifespan shutdown(cost spool/body_log drain)은 그 뒤에 돈다.
#   상한 없이 기다리면 terminationGracePeriodSeconds 의 SIGKILL 이 lifespan 보다 먼저 와서
#   진행 중 스트림의 부분 사용량과 버퍼 drain 이 전부 유실된다(R24-M1).
#   grace 예산 = preStop(15) + 이 상한 + lifespan(~40) + 여유 — values.yaml 주석 참조.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WORKERS:-${UVICORN_WORKERS:-4}} --timeout-graceful-shutdown ${UVICORN_GRACEFUL_SHUTDOWN_TIMEOUT:-240}"]
