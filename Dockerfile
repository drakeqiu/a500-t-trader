# syntax=docker/dockerfile:1
FROM python:3.14-slim

ARG APT_MIRROR
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.5 /uv /uvx /bin/
WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY wheels/ ./wheels/
ARG UV_INDEX_URL
ARG UV_HTTP_TIMEOUT
RUN --mount=type=cache,id=a500-uv-cache,target=/root/.cache/uv,sharing=locked \
    if [ -n "$UV_INDEX_URL" ]; then UV_HTTP_TIMEOUT="$UV_HTTP_TIMEOUT" uv sync --index-url "$UV_INDEX_URL" --frozen --no-dev --no-install-project; else uv sync --frozen --no-dev --no-install-project; fi
COPY src/ ./src/
COPY config/ ./config/
RUN --mount=type=cache,id=a500-uv-cache,target=/root/.cache/uv,sharing=locked \
    if [ -n "$UV_INDEX_URL" ]; then UV_HTTP_TIMEOUT="$UV_HTTP_TIMEOUT" uv sync --index-url "$UV_INDEX_URL" --frozen --no-dev; else uv sync --frozen --no-dev; fi

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1
ENTRYPOINT []
