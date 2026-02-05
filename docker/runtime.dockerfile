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

# Copy package files for dependency caching
COPY apps/zerg/frontend-web/package.json ./
COPY apps/zerg/frontend-web/bun.lock* ./

# Install dependencies
RUN bun install --frozen-lockfile || bun install

# Copy frontend source
COPY apps/zerg/frontend-web/ ./

# Build for production (same-origin mode - no cross-origin API URLs needed)
# The backend will serve both static files and API from the same origin
RUN bun run build

# =============================================================================
# Stage 2: Build Backend Dependencies
# =============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS dependencies

# Install git for cloning git-based dependencies (hatch-agent)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /repo/apps/zerg/backend

# Copy pyproject files for dependency caching
COPY apps/zerg/backend/uv.lock apps/zerg/backend/pyproject.toml ./
COPY packages/hatch-agent /repo/packages/hatch-agent

RUN uv sync --frozen --no-install-project --no-dev

# =============================================================================
# Stage 3: Build Backend Application
# =============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS backend-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /repo/apps/zerg/backend

# Copy virtual environment from dependencies stage
COPY --from=dependencies /repo/apps/zerg/backend/.venv ./.venv
COPY --from=dependencies /repo/packages /repo/packages

# Copy backend source
COPY apps/zerg/backend/ ./

# Copy shared config
COPY config/models.json /config/models.json

# Copy REAL frontend dist from frontend-builder (not placeholder)
COPY --from=frontend-builder /app/dist /repo/apps/zerg/frontend-web/dist

# Install the project
RUN uv sync --frozen --no-dev

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

# Create non-root user
RUN useradd --create-home --shell /bin/bash --uid 1000 longhouse

WORKDIR /app

# Copy backend with virtual environment
COPY --from=backend-builder --chown=longhouse:longhouse /repo/apps/zerg/backend /app

# Copy frontend dist to where backend expects it
COPY --from=frontend-builder --chown=longhouse:longhouse /app/dist /app/frontend-web/dist

# Copy config
COPY --from=backend-builder --chown=longhouse:longhouse /config /config

# Create required directories
RUN mkdir -p /app/static/avatars /data \
    && chown -R longhouse:longhouse /app/static /data \
    && chmod 755 /app/static /app/static/avatars /data

USER longhouse

# Environment
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MODELS_CONFIG_PATH="/config/models.json"

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/api/health || exit 1

EXPOSE 8000

# Start server - serves both API and frontend
CMD ["python", "-m", "zerg.cli.main", "serve", "--host", "0.0.0.0", "--port", "8000"]
