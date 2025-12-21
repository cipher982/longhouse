# React Frontend Multi-stage Dockerfile
# Development and Production targets
#
# BUILD CONTEXT: This Dockerfile expects repo root as build context
# Example: docker build -f apps/zerg/frontend-web/Dockerfile .

# Builder stage - build tokens and frontend
FROM oven/bun:alpine AS builder

WORKDIR /app

# Copy only the workspaces we need
COPY packages/design-tokens ./packages/design-tokens/
COPY apps/zerg/frontend-web ./apps/zerg/frontend-web/

# Create minimal workspace config (only declares packages we actually copied)
# This avoids "workspace not found" errors from the full monorepo package.json
RUN cat > package.json << 'EOF'
{
  "name": "@swarm/frontend-build",
  "private": true,
  "workspaces": ["packages/design-tokens", "apps/zerg/frontend-web"]
}
EOF

# Install dependencies (workspace resolution now works)
RUN bun install

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

# Build React app for production
WORKDIR /app/apps/zerg/frontend-web
RUN bun run build

# Production target - nginx serving static files
FROM nginx:alpine AS production

# Copy custom nginx configuration
COPY apps/zerg/frontend-web/nginx.conf /etc/nginx/nginx.conf

# Copy built React app
COPY --from=builder /app/apps/zerg/frontend-web/dist/ /usr/share/nginx/html/

# Health check: ensure key files exist
RUN test -f /usr/share/nginx/html/index.html

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

# Development target - Vite dev server with hot reload
FROM oven/bun:alpine AS development

WORKDIR /app

# Copy workspaces
COPY packages/design-tokens ./packages/design-tokens/
COPY apps/zerg/frontend-web ./apps/zerg/frontend-web/

# Create minimal workspace config
RUN cat > package.json << 'EOF'
{
  "name": "@swarm/frontend-build",
  "private": true,
  "workspaces": ["packages/design-tokens", "apps/zerg/frontend-web"]
}
EOF

# Install dependencies
RUN bun install

# Build tokens for dev
RUN cd packages/design-tokens && bun run build

WORKDIR /app/apps/zerg/frontend-web

EXPOSE 5173

CMD ["bun", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]
