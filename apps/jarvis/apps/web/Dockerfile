# syntax=docker/dockerfile:1.4
# Development Dockerfile for Jarvis Web UI
# Uses jarvis-specific workspace (not full monorepo) for smaller image size
FROM oven/bun:alpine

# Install curl for healthcheck
RUN apk add --no-cache curl

WORKDIR /app

# Copy jarvis workspace root (NOT the full monorepo root)
# This gives us jarvis-only workspaces, not the entire monorepo
COPY apps/jarvis/package.json ./package.json
COPY apps/jarvis/bun.lock* ./

# Copy monorepo config (models.json required by @swarm/config)
COPY config ./config

# Copy @swarm/config package (from root packages/)
COPY packages/config/package.json ./swarm-packages/config/package.json
COPY packages/config/src ./swarm-packages/config/src
# Symlink models.json so @swarm/config can import it
RUN ln -s /app/config/models.json /app/swarm-packages/config/models.json

# Copy jarvis workspace package.json files
COPY apps/jarvis/packages/core/package.json ./packages/core/package.json
COPY apps/jarvis/packages/data/local/package.json ./packages/data/local/package.json
COPY apps/jarvis/apps/web/package.json ./apps/web/package.json
COPY apps/jarvis/apps/server/package.json ./apps/server/package.json
COPY apps/jarvis/apps/native/package.json ./apps/native/package.json

# Install only jarvis dependencies (much smaller than full monorepo)
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install

# Copy jarvis source code
COPY apps/jarvis/packages ./packages
COPY apps/jarvis/configs ./configs
COPY apps/jarvis/apps/web ./apps/web

# Fail-fast verification: Ensure critical config files exist
RUN test -f /app/config/models.json || (echo "❌ FATAL: models.json not found" && exit 1)

# Set working directory to web app
WORKDIR /app/apps/web

# ARG for build-time default, ENV for runtime
ARG JARVIS_WEB_PORT=8080
ENV JARVIS_WEB_PORT=$JARVIS_WEB_PORT

# Static EXPOSE (documentation only)
EXPOSE 8080

# Copy runtime verification script
COPY apps/jarvis/apps/web/verify-config.sh /app/verify-config.sh
RUN chmod +x /app/verify-config.sh

# Run Vite dev server with config verification
CMD ["sh", "-c", "/app/verify-config.sh && bun run dev -- --port ${JARVIS_WEB_PORT} --host 0.0.0.0"]
