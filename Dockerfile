# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.12.5 AS uv
FROM livekit/livekit-server:v1.13.11 AS livekit
FROM python:3.12-slim-bookworm

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock ./
RUN --mount=type=bind,from=uv,source=/uv,target=/uv /uv sync --locked --no-dev --no-cache

COPY --from=livekit /livekit-server /usr/local/bin/livekit-server
COPY agent/ ./agent/
COPY tests/ ./tests/

RUN useradd --create-home --uid 10001 keni
USER keni

HEALTHCHECK --interval=5s --timeout=2s --start-period=30s \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8765/health', timeout=1).close()"]

CMD ["python", "-m", "agent.launch"]
