# Multi-stage build for Zerg AI Agent Platform Backend
# Optimized for production-grade caching and security

# Dependencies stage - cache Python packages efficiently
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS dependencies

# uv environment variables for optimization
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

# Set work directory
WORKDIR /app

# Cache dependencies separately from app code for better cache hits
COPY uv.lock pyproject.toml ./
RUN uv sync --frozen --no-install-project --no-dev

# Builder stage - application with dependencies
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

# uv environment variables for optimization
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy

WORKDIR /app

# Copy virtual environment from dependencies stage
COPY --from=dependencies /app/.venv /app/.venv

# Copy application source
COPY . .

# Install the project itself using cached dependencies
RUN uv sync --frozen --no-dev

# Production stage - minimal distroless-style runtime
FROM python:3.12-slim-bookworm AS production

# Install only essential runtime dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpq5 \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash --uid 1000 zerg

# Set work directory
WORKDIR /app

# Copy application and virtual environment from builder
COPY --from=builder --chown=zerg:zerg /app /app

# Create required directories with proper permissions
RUN mkdir -p /app/static/avatars \
    && chown zerg:zerg /app/static \
    && chown zerg:zerg /app/static/avatars \
    && chmod 755 /app/static \
    && chmod 755 /app/static/avatars

# Switch to non-root user
USER zerg

# Add virtual environment to PATH
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

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
    && rm -rf /var/lib/apt/lists/*

# Copy the virtual environment to /opt/venv to avoid volume mount conflicts
RUN cp -r /app/.venv /opt/venv && \
    find /opt/venv/bin -type f -exec sed -i 's|#!/app/.venv/bin/python|#!/opt/venv/bin/python|g' {} \;

# Install dev dependencies using uv (available in builder stage)
RUN uv sync --frozen

# Create non-root user (same as production)
RUN useradd --create-home --shell /bin/bash --uid 1000 zerg || true

# Create required directories with proper permissions
RUN mkdir -p /app/static/avatars \
    && chown zerg:zerg /app/static \
    && chown zerg:zerg /app/static/avatars

# Switch back to non-root user
USER zerg

# Add virtual environment to PATH for development (using /opt/venv)
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH="/app" \
    PYTHONUNBUFFERED=1

# Development command with hot reload - use uvicorn directly
CMD ["uvicorn", "zerg.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--reload", \
     "--reload-dir", "/app/zerg", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*"]
