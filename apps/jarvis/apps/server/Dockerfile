# syntax=docker/dockerfile:1.4
# Dockerfile for Jarvis Server (OpenAI bridge + MCP tools)
# Uses jarvis-specific workspace (not full monorepo) for smaller image size
FROM oven/bun:alpine

# Install curl for healthcheck
RUN apk add --no-cache curl

WORKDIR /app

# Copy jarvis workspace root (NOT the full monorepo root)
# This gives us jarvis-only workspaces, not the entire monorepo
COPY apps/jarvis/package.json ./package.json
COPY apps/jarvis/bun.lock* ./

# Copy jarvis workspace package.json files
COPY apps/jarvis/packages/core/package.json ./packages/core/package.json
COPY apps/jarvis/packages/data/local/package.json ./packages/data/local/package.json
COPY apps/jarvis/apps/server/package.json ./apps/server/package.json
COPY apps/jarvis/apps/web/package.json ./apps/web/package.json
COPY apps/jarvis/apps/native/package.json ./apps/native/package.json

# Install only jarvis dependencies (much smaller than full monorepo)
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install

# Copy jarvis source code
COPY apps/jarvis/packages ./packages
COPY apps/jarvis/apps/server ./apps/server

# Set working directory to server app
WORKDIR /app/apps/server

# ARG for build-time default, ENV for runtime
ARG JARVIS_SERVER_PORT=8787
ENV JARVIS_SERVER_PORT=$JARVIS_SERVER_PORT

# Static EXPOSE (documentation only)
EXPOSE 8787

# Health check with configurable port
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${JARVIS_SERVER_PORT}/session || exit 1

CMD ["bun", "run", "server.js"]
