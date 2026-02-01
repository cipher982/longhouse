# Multi-stage build for Zerg AI Agent Platform Backend
# Optimized for production-grade caching and security
#
# BUILD CONTEXT: This Dockerfile expects repo root as build context
# Example: docker build -f docker/backend.dockerfile .

# Dependencies stage - cache Python packages efficiently
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS dependencies

# Install git for cloning git-based dependencies (hatch-agent)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# uv environment variables for optimization
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Set work directory - mirror repo structure for relative path deps
WORKDIR /repo/apps/zerg/backend

# Cache dependencies separately from app code for better cache hits
# Copy pyproject.toml to correct path so relative deps resolve
COPY apps/zerg/backend/uv.lock apps/zerg/backend/pyproject.toml ./
# Copy hatch-agent local package (pyproject.toml references ../../../packages/hatch-agent)
COPY packages/hatch-agent /repo/packages/hatch-agent
RUN uv sync --frozen --no-install-project --no-dev

# Builder stage - application with dependencies
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# Install build-time dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# uv environment variables for optimization
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Mirror repo structure for relative path deps
WORKDIR /repo/apps/zerg/backend

# Copy virtual environment from dependencies stage
COPY --from=dependencies /repo/apps/zerg/backend/.venv ./.venv

# Copy hatch-agent for uv sync
COPY --from=dependencies /repo/packages /repo/packages

# Copy application source (from repo root context)
COPY apps/zerg/backend/ ./

# Copy shared config (models.json) - required for model configuration
COPY config/models.json /config/models.json

# Create placeholder for frontend dist (pyproject.toml force-include expects it)
# In Docker, frontend is served separately, but hatch build still needs the path
RUN mkdir -p /repo/apps/zerg/frontend-web/dist && \
    echo '<!DOCTYPE html><html><body>Frontend served separately</body></html>' > /repo/apps/zerg/frontend-web/dist/index.html

# Install the project itself using cached dependencies
RUN uv sync --frozen --no-dev

# Production stage - minimal distroless-style runtime
FROM python:3.12-slim-bookworm AS production

# Install only essential runtime dependencies (including Node.js for Claude CLI)
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    ca-certificates \
    openssh-client \
    git \
    nodejs \
    npm \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Install Claude Code CLI globally for QA agent
RUN npm install -g @anthropic-ai/claude-code@2.1.17 \
    && npm cache clean --force

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash --uid 1000 zerg

# Set work directory
WORKDIR /app

# Copy application and virtual environment from builder
COPY --from=builder --chown=zerg:zerg /repo/apps/zerg/backend /app

# Copy config from builder stage
COPY --from=builder --chown=zerg:zerg /config /config

# Create required directories with proper permissions
RUN mkdir -p /app/static/avatars \
    && mkdir -p /data \
    && chown zerg:zerg /app/static \
    && chown zerg:zerg /app/static/avatars \
    && chown zerg:zerg /data \
    && chmod 755 /app/static \
    && chmod 755 /app/static/avatars \
    && chmod 755 /data

# Switch to non-root user
USER zerg

# Add virtual environment to PATH and set config path
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MODELS_CONFIG_PATH="/config/models.json"

# Health check with retry logic
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || curl -f http://localhost:8000/ || exit 1

# Expose port
EXPOSE 8000

# Start the application with migrations
CMD ["./start.sh"]

# Development target for local development
FROM builder AS development

# Switch back to root for dev dependencies
USER root

# Install development tools
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    make \
    curl \
    openssh-client \
    libpq5 \
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual environment to /opt/venv to avoid volume mount conflicts
RUN cp -r /repo/apps/zerg/backend/.venv /opt/venv && \
    find /opt/venv/bin -type f -exec sed -i 's|#!/repo/apps/zerg/backend/.venv/bin/python|#!/opt/venv/bin/python|g' {} \;

# Install dev dependencies using uv (available in builder stage)
RUN uv sync --frozen

# Create non-root user (same as production)
RUN useradd --create-home --shell /bin/bash --uid 1000 zerg || true

# Set up /app symlink for compatibility
RUN ln -s /repo/apps/zerg/backend /app

# Create required directories with proper permissions
RUN mkdir -p /repo/apps/zerg/backend/static/avatars \
    && chown zerg:zerg /repo/apps/zerg/backend/static \
    && chown zerg:zerg /repo/apps/zerg/backend/static/avatars

# Switch back to non-root user
USER zerg

# Add virtual environment to PATH for development (using /opt/venv)
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/repo/apps/zerg/backend" \
    PYTHONUNBUFFERED=1

# Development command - run migrations then uvicorn with hot reload
CMD ["./start-dev.sh"]
