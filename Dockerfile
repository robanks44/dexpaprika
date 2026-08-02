# Multi-stage, non-root, pinned base (ENGINEERING_STANDARDS §6).
# One artifact runs locally and in a container unchanged; scheduling stays
# external (cron/cloud scheduler calls the CLI).

FROM python:3.13-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev

FROM python:3.13-slim-bookworm
RUN groupadd -r app && useradd -r -g app -d /home/app -m app
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DEXPAPRIKA_DATA_DIR=/data
VOLUME ["/data"]
USER app
ENTRYPOINT ["dexpaprika"]
CMD ["status", "--json"]
