# Unified Runtime Dockerfile for Longhouse
# Single container serving both frontend (static) and backend (API)
#
# BUILD CONTEXT: repo root
# Example: docker build -f docker/runtime.dockerfile -t longhouse-runtime .
#
# For hosted instances provisioned by control plane.
# Backend serves frontend via StaticFiles mount (no nginx needed).

# =============================================================================
# Stage 1: Build Frontend
# =============================================================================
FROM oven/bun:alpine AS frontend-builder

WORKDIR /app

# Copy root lockfile + workspace package.json for dependency caching
# bun.lock lives at monorepo root; no per-workspace lockfile exists
COPY bun.lock ./
COPY web/package.json ./

# Install dependencies (no --frozen-lockfile: root lockfile covers all workspaces
# but Docker only has this one package.json, causing a mismatch. Lockfile still
# guides version resolution without strict mode.)
RUN bun install

# Copy frontend source
COPY web/ ./

# Build for production (same-origin mode - no cross-origin API URLs needed)
# The backend will serve both static files and API from the same origin
RUN bun run build

# =============================================================================
# Stage 1.5: Build pysqlite3 wheel with pinned SQLite amalgamation
# =============================================================================
FROM python:3.12-slim-bookworm AS pysqlite-builder

ARG SQLITE_VERSION=3510300
ARG SQLITE_SHA3=581215771b32ea4c4062e6fb9842c4aa43d0a7fb2b6670ff6fa4ebb807781204

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp
RUN pip install --upgrade setuptools wheel \
    && curl -fsSLO "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && test "$(openssl dgst -sha3-256 "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" | awk '{print $2}')" = "${SQLITE_SHA3}" \
    && tar -xzf "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && pip download --no-binary=:all: pysqlite3==0.6.0 \
    && tar -xzf pysqlite3-0.6.0.tar.gz \
    && cp "sqlite-autoconf-${SQLITE_VERSION}/sqlite3.c" "sqlite-autoconf-${SQLITE_VERSION}/sqlite3.h" pysqlite3-0.6.0/ \
    && cd pysqlite3-0.6.0 && python setup.py bdist_wheel \
    && mkdir -p /dist && cp dist/*.whl /dist/

# =============================================================================
# Stage 2: Build Backend Dependencies
# =============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS dependencies

# Install git for cloning git-based dependencies (hatch-agent)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /repo/server

# Copy pyproject files for dependency caching
COPY server/uv.lock server/pyproject.toml ./

RUN uv sync --frozen --no-install-project --no-dev

# =============================================================================
# Stage 3: Build Backend Application
# =============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS backend-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /repo/server

# Copy virtual environment from dependencies stage
COPY --from=dependencies /repo/server/.venv ./.venv

# Copy backend source
COPY server/ ./

# Copy shared config
COPY config/models.json /config/models.json

# Copy REAL frontend dist from frontend-builder (not placeholder)
COPY --from=frontend-builder /app/dist /repo/web/dist

# Install the project + pysqlite3 wheel (statically links modern SQLite)
COPY --from=pysqlite-builder /dist/ /tmp/pysqlite3-dist/
RUN uv sync --frozen --no-dev \
    && uv pip install /tmp/pysqlite3-dist/*.whl \
    && PYTHONPATH=/repo/server ./.venv/bin/python -c "\
import pysqlite3; v = pysqlite3.sqlite_version; \
parts = tuple(int(x) for x in v.split('.')); \
assert parts >= (3, 35, 0), f'SQLite {v} < 3.35.0'; \
conn = pysqlite3.connect(':memory:'); \
conn.execute('create virtual table t using fts5(x)'); \
conn.close(); print(f'pysqlite3 OK: SQLite {v}, FTS5 available')"

# =============================================================================
# Stage 4: Production Runtime
# =============================================================================
FROM python:3.12-slim-bookworm AS production

# Install runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    ca-certificates \
    openssh-client \
    git \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Node.js 22 LTS (required by Claude Code CLI for session continuation)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@latest \
    && claude --version

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 longhouse

WORKDIR /app

# Copy backend with virtual environment (includes pysqlite3 from builder).
# build-identity.json is already staged into server/zerg/ by
# scripts/build/generate_build_identity.py before the Docker build
# context is sent, so importlib.resources.files("zerg") / "build_identity.json"
# resolves inside the container with no extra COPY.
COPY --from=backend-builder --chown=longhouse:longhouse /repo/server /app

# Copy frontend dist to where backend expects it
COPY --from=frontend-builder --chown=longhouse:longhouse /app/dist /app/web/dist

# Copy config
COPY --from=backend-builder --chown=longhouse:longhouse /config /config

# Bootstrap pip in the venv so job packs can pip-install their own deps at startup
RUN /app/.venv/bin/python -m ensurepip --default-pip 2>/dev/null || true

# Create required directories
RUN mkdir -p /app/static/avatars /data \
    && chown -R longhouse:longhouse /app/static /data \
    && chmod 755 /app/static /app/static/avatars /data

# Entrypoint script (decodes SSH key from env var)
COPY --chown=root:root docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER longhouse

# Environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MODELS_CONFIG_PATH="/config/models.json" \
    LONGHOUSE_RUNTIME_PORT="8000"

# NOTE: this image binds 0.0.0.0, so the public-bind safety gate refuses to
# start when auth is disabled. A public no-auth deployment (e.g. the maintainer's
# demo behind a TLS proxy) must opt in by setting LONGHOUSE_ALLOW_PUBLIC_NO_AUTH=1
# in the *deployment* environment — intentionally NOT baked into the image so a
# pulled image is not public-no-auth by default.

# Health check — /api/readyz returns 503 on unhealthy (unlike /api/health which always 200s)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD sh -lc 'curl -f "http://localhost:${LONGHOUSE_RUNTIME_PORT}/api/readyz" || exit 1'

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
# Start server - serves both API and frontend
CMD ["sh", "-lc", "/app/.venv/bin/python -m zerg.cli.main serve --host 0.0.0.0 --port ${LONGHOUSE_RUNTIME_PORT}"]
