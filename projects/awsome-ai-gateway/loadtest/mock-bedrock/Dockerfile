FROM python:3.11-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml ./
RUN uv pip install --system fastapi uvicorn
COPY main.py eventstream.py ./

# 부하테스트 전용 mock — 클러스터 배포 시 PodSecurity restricted 와 호환되도록
# 기존 mock vLLM 이미지와 같은 비특권 사용자 규약을 따른다.
RUN groupadd --system --gid 10001 appgroup \
    && useradd  --system --uid 10001 --gid appgroup --create-home --home-dir /home/appuser appuser
USER 10001:10001
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
