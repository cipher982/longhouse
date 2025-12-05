# Development Dockerfile for Jarvis Web UI
FROM oven/bun:alpine

WORKDIR /app

# Copy root lockfile and package.json for workspace resolution
COPY bun.lock package.json ./

# Copy Jarvis workspace package files
COPY apps/jarvis/package.json ./apps/jarvis/package.json
COPY apps/jarvis/packages ./apps/jarvis/packages
COPY apps/jarvis/apps/web/package.json ./apps/jarvis/apps/web/package.json
COPY apps/jarvis/apps/server/package.json ./apps/jarvis/apps/server/package.json
COPY apps/jarvis/apps/native/package.json ./apps/jarvis/apps/native/package.json

# Install all dependencies
RUN bun install

# Copy web app source
COPY apps/jarvis/apps/web ./apps/jarvis/apps/web

# Set working directory to web app
WORKDIR /app/apps/jarvis/apps/web

# ARG for build-time default, ENV for runtime
ARG JARVIS_WEB_PORT=8080
ENV JARVIS_WEB_PORT=$JARVIS_WEB_PORT

# Static EXPOSE (documentation only)
EXPOSE 8080

# Run Vite dev server
CMD ["sh", "-c", "bun run dev -- --port ${JARVIS_WEB_PORT} --host 0.0.0.0"]
