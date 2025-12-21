# React Frontend Multi-stage Dockerfile
# Development and Production targets
#
# BUILD CONTEXT: This Dockerfile expects repo root as build context
# Example: docker build -f apps/zerg/frontend-web/Dockerfile .

# Builder stage - build tokens and frontend
FROM oven/bun:alpine AS builder

WORKDIR /app

# Copy workspace config for proper dependency resolution
COPY package.json bun.lock ./

# Copy design-tokens package (workspace dependency)
COPY packages/design-tokens ./packages/design-tokens/

# Copy frontend-web app
COPY apps/zerg/frontend-web ./apps/zerg/frontend-web/

# Install all workspace dependencies
RUN bun install --frozen-lockfile

# Build design tokens first (frontend depends on it)
RUN cd packages/design-tokens && bun run build

# Build args for Vite env vars (baked into JS bundle at build time)
ARG VITE_UMAMI_WEBSITE_ID
ARG VITE_UMAMI_SCRIPT_SRC
ARG VITE_UMAMI_DOMAINS

# Make args available as env vars during build
ENV VITE_UMAMI_WEBSITE_ID=$VITE_UMAMI_WEBSITE_ID
ENV VITE_UMAMI_SCRIPT_SRC=$VITE_UMAMI_SCRIPT_SRC
ENV VITE_UMAMI_DOMAINS=$VITE_UMAMI_DOMAINS

# Build React app for production (skip the copy:tokens prebuild - we import from package)
WORKDIR /app/apps/zerg/frontend-web
RUN bun run build

# Production target - nginx serving static files
FROM nginx:alpine AS production

# Copy custom nginx configuration
COPY apps/zerg/frontend-web/nginx.conf /etc/nginx/nginx.conf

# Copy built React app (production build uses base: "/" so copy to root)
COPY --from=builder /app/apps/zerg/frontend-web/dist/ /usr/share/nginx/html/

# Health check: ensure key files exist
RUN test -f /usr/share/nginx/html/index.html

EXPOSE 80

# Run nginx in foreground - nginx master process will handle SIGTERM gracefully
# The key is running it directly (not through shell) so signals work properly
CMD ["nginx", "-g", "daemon off;"]

# Development target - Vite dev server with hot reload
FROM oven/bun:alpine AS development

WORKDIR /app

# Copy workspace config
COPY package.json bun.lock ./
COPY packages/design-tokens ./packages/design-tokens/
COPY apps/zerg/frontend-web ./apps/zerg/frontend-web/

# Install dependencies
RUN bun install --frozen-lockfile

# Build tokens for dev
RUN cd packages/design-tokens && bun run build

WORKDIR /app/apps/zerg/frontend-web

# Expose Vite dev server port
EXPOSE 5173

# Run Vite dev server with hot reload using Bun
# --host 0.0.0.0 allows access from outside the container
# --port 5173 explicitly sets the port to match EXPOSE directive
CMD ["bun", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
