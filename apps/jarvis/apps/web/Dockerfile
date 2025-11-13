# Development Dockerfile for Jarvis Web UI
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

# Copy web app
WORKDIR /app/apps/web
COPY apps/jarvis/apps/web .

# ARG for build-time default, ENV for runtime
ARG JARVIS_WEB_PORT=8080
ENV JARVIS_WEB_PORT=$JARVIS_WEB_PORT

# Static EXPOSE (documentation only)
EXPOSE 8080

# Shell form CMD so environment variable expands
CMD yarn run dev -- --port ${JARVIS_WEB_PORT} --host 0.0.0.0
