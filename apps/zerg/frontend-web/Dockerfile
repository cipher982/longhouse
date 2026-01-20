# React Frontend Dockerfile
# Simple single-app build - no workspace complexity

# Builder stage - build frontend for production
FROM oven/bun:alpine AS builder

WORKDIR /app

# Copy package files first for better caching
COPY apps/zerg/frontend-web/package.json ./
COPY apps/zerg/frontend-web/bun.lock* ./

# Install dependencies
RUN bun install --frozen-lockfile || bun install

# Copy source
COPY apps/zerg/frontend-web/ ./

# Build args for Vite env vars (baked into JS bundle at build time)
ARG VITE_API_BASE_URL
ARG VITE_WS_BASE_URL
ARG VITE_UMAMI_WEBSITE_ID
ARG VITE_UMAMI_SCRIPT_SRC
ARG VITE_UMAMI_DOMAINS

ENV VITE_API_BASE_URL=$VITE_API_BASE_URL
ENV VITE_WS_BASE_URL=$VITE_WS_BASE_URL
ENV VITE_UMAMI_WEBSITE_ID=$VITE_UMAMI_WEBSITE_ID
ENV VITE_UMAMI_SCRIPT_SRC=$VITE_UMAMI_SCRIPT_SRC
ENV VITE_UMAMI_DOMAINS=$VITE_UMAMI_DOMAINS

# Build
RUN bun run build

# Production target - nginx serving static files
FROM nginx:alpine AS production

COPY apps/zerg/frontend-web/nginx.conf /etc/nginx/nginx.conf
COPY apps/zerg/frontend-web/docker-entrypoint.sh /docker-entrypoint.sh
COPY --from=builder /app/dist/ /usr/share/nginx/html/

RUN test -f /usr/share/nginx/html/index.html && \
    chmod +x /docker-entrypoint.sh

EXPOSE 80
ENTRYPOINT ["/docker-entrypoint.sh"]

# Development target - Vite dev server with hot reload
FROM oven/bun:alpine AS development

WORKDIR /app

# Copy package files
COPY apps/zerg/frontend-web/package.json ./
COPY apps/zerg/frontend-web/bun.lock* ./

# Install dependencies
RUN bun install --frozen-lockfile || bun install

# Copy source (will be overwritten by volume mount in dev)
COPY apps/zerg/frontend-web/ ./

EXPOSE 5173
CMD ["bun", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
