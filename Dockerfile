# Multi-stage, non-root, pinned base (ENGINEERING_STANDARDS §6).
# One artifact runs locally and in a container unchanged; scheduling runs
# in-container via `dexpaprika scheduler run` (S11) or externally (cron).
#
# Building behind a TLS-intercepting egress proxy (CI/sandbox): drop the
# proxy CA into the build context as `build-ca.crt` (gitignored) — the
# optional-glob COPY below picks it up; normal builds are unaffected.

FROM python:3.13-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /uvx /usr/local/bin/
WORKDIR /app
COPY pyproject.toml uv.lock build-ca.crt* ./
RUN if [ -f build-ca.crt ]; then cat build-ca.crt >> /etc/ssl/certs/ca-certificates.crt; fi
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
COPY README.md ./
RUN uv sync --frozen --no-dev && rm -f build-ca.crt

FROM python:3.13-slim-bookworm
RUN groupadd -r app && useradd -r -g app -d /home/app -m app \
    && mkdir -p /data && chown app:app /data
WORKDIR /app
COPY --from=builder --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DEXPAPRIKA_DATA_DIR=/data
VOLUME ["/data"]
USER app
ENTRYPOINT ["dexpaprika"]
CMD ["status", "--json"]
