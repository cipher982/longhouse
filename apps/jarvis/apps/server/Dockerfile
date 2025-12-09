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

# Copy shared config (needed by @swarm/config)
COPY config ./config

# Copy @swarm/config package (from root packages/)
COPY packages/config/package.json ./swarm-packages/config/package.json
COPY packages/config/src ./swarm-packages/config/src
# Symlink models.json so @swarm/config can import it
RUN ln -s /app/config/models.json /app/swarm-packages/config/models.json

# Copy jarvis workspace package.json files
COPY apps/jarvis/packages/core/package.json ./packages/core/package.json
COPY apps/jarvis/packages/data/local/package.json ./packages/data/local/package.json
COPY apps/jarvis/apps/server/package.json ./apps/server/package.json
COPY apps/jarvis/apps/web/package.json ./apps/web/package.json
COPY apps/jarvis/apps/native/package.json ./apps/native/package.json

# Copy jarvis source code BEFORE install (needed for workspace resolution)
COPY apps/jarvis/packages ./packages
COPY apps/jarvis/apps/server ./apps/server

# Install with all workspace packages present
RUN --mount=type=cache,target=/root/.bun/install/cache \
    bun install

# Workaround: Bun looks for idb in packages/data/local/node_modules, create symlink
RUN mkdir -p /app/packages/data/local/node_modules && \
    ln -s /app/node_modules/idb /app/packages/data/local/node_modules/idb

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
