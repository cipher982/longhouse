# Dockerfile for Jarvis Server (OpenAI bridge + MCP tools)
FROM node:20-alpine

# Install Yarn
RUN corepack enable && corepack prepare yarn@4.5.0 --activate

# Install from root workspace
WORKDIR /app

# Copy root package files to install all workspaces
COPY apps/jarvis/package.json apps/jarvis/yarn.lock ./
COPY apps/jarvis/packages ./packages

# Install all dependencies (including workspaces) using Yarn
RUN yarn install

# Copy server app
WORKDIR /app/apps/server
COPY apps/jarvis/apps/server .

# ARG for build-time default, ENV for runtime
ARG JARVIS_SERVER_PORT=8787
ENV JARVIS_SERVER_PORT=$JARVIS_SERVER_PORT

# Static EXPOSE (documentation only)
EXPOSE 8787

# Health check with configurable port
HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${JARVIS_SERVER_PORT}/session || exit 1

CMD ["node", "server.js"]
