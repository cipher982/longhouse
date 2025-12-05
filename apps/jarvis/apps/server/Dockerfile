# Dockerfile for Jarvis Server (OpenAI bridge + MCP tools)
FROM oven/bun:alpine

# Install curl for healthcheck
RUN apk add --no-cache curl

WORKDIR /app

# Copy root lockfile and package.json for workspace resolution
COPY bun.lock package.json ./

# Copy Jarvis workspace package files
COPY apps/jarvis/package.json ./apps/jarvis/package.json
COPY apps/jarvis/packages ./apps/jarvis/packages
COPY apps/jarvis/apps/server/package.json ./apps/jarvis/apps/server/package.json
COPY apps/jarvis/apps/web/package.json ./apps/jarvis/apps/web/package.json
COPY apps/jarvis/apps/native/package.json ./apps/jarvis/apps/native/package.json

# Install all dependencies
RUN bun install

# Copy server app source
COPY apps/jarvis/apps/server ./apps/jarvis/apps/server

# Set working directory to server app
WORKDIR /app/apps/jarvis/apps/server

# ARG for build-time default, ENV for runtime
ARG JARVIS_SERVER_PORT=8787
ENV JARVIS_SERVER_PORT=$JARVIS_SERVER_PORT

# Static EXPOSE (documentation only)
EXPOSE 8787

# Health check with configurable port
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${JARVIS_SERVER_PORT}/session || exit 1

CMD ["bun", "run", "server.js"]
