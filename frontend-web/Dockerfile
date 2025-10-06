# React Frontend Production Dockerfile
FROM node:20-alpine AS builder

WORKDIR /app

# Copy package files and install dependencies
COPY package*.json ./
RUN npm install

# Copy source code
COPY . .

# Build React app for production
RUN npm run build

# Production server
FROM nginx:alpine

# Copy custom nginx configuration
COPY nginx.conf /etc/nginx/nginx.conf

# Copy built React app (production build uses base: "/" so copy to root)
COPY --from=builder /app/dist/ /usr/share/nginx/html/

# Health check: ensure key files exist
RUN test -f /usr/share/nginx/html/index.html

EXPOSE 80