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
# Stage 1.5: Build pinned SQLite runtime
# =============================================================================
FROM debian:bookworm-slim AS sqlite-builder

ARG SQLITE_VERSION=3510300
ARG SQLITE_SHA3=581215771b32ea4c4062e6fb9842c4aa43d0a7fb2b6670ff6fa4ebb807781204

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

RUN curl -fsSLO "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && test "$(openssl dgst -sha3-256 "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" | awk '{print $2}')" = "${SQLITE_SHA3}" \
    && tar -xzf "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && cd "sqlite-autoconf-${SQLITE_VERSION}" \
    && ./configure --prefix=/usr/local --disable-static --enable-threadsafe \
    && make -j"$(nproc)" \
    && make install DESTDIR=/sqlite-dist

# =============================================================================
# Stage 1.6: Build pinned Python SQLite wheel with bundled amalgamation
# =============================================================================
FROM python:3.12-slim-bookworm AS pysqlite-builder

ARG SQLITE_VERSION=3510300
ARG SQLITE_SHA3=581215771b32ea4c4062e6fb9842c4aa43d0a7fb2b6670ff6fa4ebb807781204

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    openssl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

RUN curl -fsSLO "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && test "$(openssl dgst -sha3-256 "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" | awk '{print $2}')" = "${SQLITE_SHA3}" \
    && tar -xzf "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && python -m pip install --upgrade pip setuptools wheel \
    && pip download --no-binary=:all: pysqlite3==0.6.0 \
    && tar -xzf pysqlite3-0.6.0.tar.gz \
    && cp "sqlite-autoconf-${SQLITE_VERSION}/sqlite3.c" "sqlite-autoconf-${SQLITE_VERSION}/sqlite3.h" pysqlite3-0.6.0/ \
    && cd pysqlite3-0.6.0 \
    && python setup.py bdist_wheel \
    && mkdir -p /dist \
    && cp dist/*.whl /dist/

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

COPY --from=sqlite-builder /sqlite-dist/usr/local/ /usr/local/
RUN ldconfig

# Copy virtual environment from dependencies stage
COPY --from=dependencies /repo/server/.venv ./.venv

# Copy backend source
COPY server/ ./
COPY control-plane/longhouse_shared /app/longhouse_shared

# Copy shared config
COPY config/models.json /config/models.json

# Copy REAL frontend dist from frontend-builder (not placeholder)
COPY --from=frontend-builder /app/dist /repo/web/dist

# Install the project
COPY --from=pysqlite-builder /dist/ /tmp/pysqlite3-dist/
RUN uv sync --frozen --no-dev \
    && ./.venv/bin/python -m ensurepip --default-pip \
    && ./.venv/bin/pip install --no-cache-dir /tmp/pysqlite3-dist/*.whl \
    && PYTHONPATH=/repo/server ./.venv/bin/python - <<'PY'
from zerg.bootstrap_sqlite import bootstrap

bootstrap()
import sqlite3

assert sqlite3.sqlite_version == "3.51.3", sqlite3.sqlite_version
with sqlite3.connect(":memory:") as conn:
    conn.execute("create virtual table t using fts5(x)")
PY

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

COPY --from=sqlite-builder /sqlite-dist/usr/local/ /usr/local/
COPY --from=pysqlite-builder /dist/ /tmp/pysqlite3-dist/
RUN ldconfig \
    && python -m pip install --no-cache-dir /tmp/pysqlite3-dist/*.whl \
    && python - <<'PY'
import pysqlite3

assert pysqlite3.sqlite_version == "3.51.3", pysqlite3.sqlite_version
with pysqlite3.connect(":memory:") as conn:
    conn.execute("create virtual table t using fts5(x)")
PY

# Install Node.js 22 LTS (required by Claude Code CLI for session continuation)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@latest \
    && claude --version

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 longhouse

WORKDIR /app

# Copy backend with virtual environment
COPY --from=backend-builder --chown=longhouse:longhouse /repo/server /app
COPY --from=backend-builder --chown=longhouse:longhouse /app/longhouse_shared /app/longhouse_shared

# Copy frontend dist to where backend expects it
COPY --from=frontend-builder --chown=longhouse:longhouse /app/dist /app/web/dist

# Copy config
COPY --from=backend-builder --chown=longhouse:longhouse /config /config
COPY --from=backend-builder --chown=longhouse:longhouse /tmp/pysqlite3-dist /tmp/pysqlite3-dist

# Bootstrap pip in the venv so job packs can pip-install their own deps at startup
RUN /app/.venv/bin/python -m ensurepip --default-pip 2>/dev/null || true \
    && /app/.venv/bin/python -m pip install --no-cache-dir /tmp/pysqlite3-dist/*.whl \
    && PYTHONPATH=/app /app/.venv/bin/python - <<'PY'
from zerg.bootstrap_sqlite import bootstrap

bootstrap()
import sqlite3

assert sqlite3.sqlite_version == "3.51.3", sqlite3.sqlite_version
with sqlite3.connect(":memory:") as conn:
    conn.execute("create virtual table t using fts5(x)")
PY

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
    MODELS_CONFIG_PATH="/config/models.json"

# Health check — /api/readyz returns 503 on unhealthy (unlike /api/health which always 200s)
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/api/readyz || exit 1

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
# Start server - serves both API and frontend
CMD ["python", "-m", "zerg.cli.main", "serve", "--host", "0.0.0.0", "--port", "8000"]
