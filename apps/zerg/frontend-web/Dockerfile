# React Frontend Multi-stage Dockerfile
# Development and Production targets

# Builder stage - shared dependencies
FROM oven/bun:alpine AS builder

WORKDIR /app

# Copy package files and install dependencies
COPY package*.json ./
RUN bun install --frozen-lockfile

# Copy source code
COPY . .

# Build React app for production
RUN bun run build

# Production target - nginx serving static files
FROM nginx:alpine AS production

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy built React app (production build uses base: "/" so copy to root)
COPY --from=builder /app/dist/ /usr/share/nginx/html/

# Health check: ensure key files exist
RUN test -f /usr/share/nginx/html/index.html

EXPOSE 80

# Run nginx in foreground - nginx master process will handle SIGTERM gracefully
# The key is running it directly (not through shell) so signals work properly
CMD ["nginx", "-g", "daemon off;"]

# Development target - Vite dev server with hot reload
FROM oven/bun:alpine AS development

WORKDIR /app

# Install dependencies
COPY package*.json ./
RUN bun install --frozen-lockfile

# Copy source code (will be overridden by volume mount in docker-compose)
COPY . .

# Expose Vite dev server port
EXPOSE 5173

# Run Vite dev server with hot reload using Bun
# --host 0.0.0.0 allows access from outside the container
# --port 5173 explicitly sets the port to match EXPOSE directive
CMD ["bun", "run", "dev", "--", "--host", "0.0.0.0", "--port", "5173"]