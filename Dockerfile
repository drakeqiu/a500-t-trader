# syntax=docker/dockerfile:1
FROM python:3.14-slim

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
RUN if [ -n "$UV_INDEX_URL" ]; then \
        python -m pip install --no-cache-dir --timeout "${UV_HTTP_TIMEOUT:-180}" --index-url "$UV_INDEX_URL" --find-links=/app/wheels /app/dist/*.whl; \
    else \
        python -m pip install --no-cache-dir --find-links=/app/wheels /app/dist/*.whl; \
    fi

ENV PYTHONUNBUFFERED=1
ENTRYPOINT []
