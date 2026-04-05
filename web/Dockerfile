# React Frontend Dockerfile
# Simple single-app build - no workspace complexity
# Cache bust: 2026-01-24 - force rebuild for mobile CSS fix

# Builder stage - build frontend for production
FROM oven/bun:alpine AS builder

# Cache-bust arg - change value to force full rebuild
ARG CACHE_BUST=20260124-v2
RUN echo "Cache bust: $CACHE_BUST"

WORKDIR /app

# Copy package files first for better caching
COPY web/package.json ./
COPY web/bun.lock* ./

# Install dependencies
RUN bun install --frozen-lockfile || bun install

# Copy source
COPY web/ ./

# Build args for Vite env vars (legacy split-deploy API/WS fallback)
ARG VITE_API_BASE_URL
ARG VITE_WS_BASE_URL

ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_WS_BASE_URL=$VITE_WS_BASE_URL

# Build
RUN bun run build

# Production target - nginx serving static files
FROM nginx:alpine AS production

COPY web/nginx.conf.template /etc/nginx/nginx.conf.template
COPY web/docker-entrypoint.sh /docker-entrypoint.sh
COPY --from=builder /app/dist/ /usr/share/nginx/html/

RUN test -f /usr/share/nginx/html/index.html && \
    chmod +x /docker-entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/docker-entrypoint.sh"]

# Development target - Vite dev server with hot reload
FROM oven/bun:alpine AS development

WORKDIR /app

# Copy package files
COPY web/package.json ./
COPY web/bun.lock* ./

# Install dependencies
RUN bun install --frozen-lockfile || bun install

# Copy source (will be overwritten by volume mount in dev)
COPY web/ ./

EXPOSE 5173
CMD ["bun", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
