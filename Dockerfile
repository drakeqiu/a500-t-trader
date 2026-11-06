FROM python:3.14-slim

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY wheels/ ./wheels/
ARG UV_INDEX_URL
RUN if [ -n "$UV_INDEX_URL" ]; then uv sync --index-url "$UV_INDEX_URL" --frozen --no-dev --no-install-project; else uv sync --frozen --no-dev --no-install-project; fi
COPY src/ ./src/
COPY config/ ./config/
RUN if [ -n "$UV_INDEX_URL" ]; then uv sync --index-url "$UV_INDEX_URL" --frozen --no-dev; else uv sync --frozen --no-dev; fi

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
ENTRYPOINT []
