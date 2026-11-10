# syntax=docker/dockerfile:1
FROM ghcr.io/astral-sh/uv:0.13.7 AS uv
FROM python:3.14-slim
COPY --from=uv /uv /uvx /bin/

ARG APT_MIRROR
RUN if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends libgssapi-krb5-2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY config/ ./config/
COPY wheels/ ./wheels/
COPY dist/*.whl ./dist/
ARG UV_INDEX_URL
ARG UV_HTTP_TIMEOUT
ENV UV_HTTP_TIMEOUT=${UV_HTTP_TIMEOUT:-180}
RUN uv pip install --system --no-cache --find-links=/app/wheels /app/dist/*.whl

ENV PYTHONUNBUFFERED=1
ENTRYPOINT []
