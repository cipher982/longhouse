# React Frontend Production Dockerfile
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files
COPY package*.json ./
RUN npm ci --only=production

# Copy source code
COPY . .

# Build React app for production
RUN npm run build

# Production server
FROM nginx:alpine

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy built React app
COPY --from=builder /app/dist/ /usr/share/nginx/html/

# Health check: ensure key files exist
RUN test -f /usr/share/nginx/html/index.html

EXPOSE 80