# Build from the repo root so the image installs from the workspace lockfile:
#   docker build -f runner/Dockerfile -t longhouse/runner:latest .
FROM oven/bun:1.2.20@sha256:78f46b81b82d767ee8d729411f6f95089a403c21f17c20a5789df00263d7c5b5

WORKDIR /app

# Bun resolves the root lockfile against every declared workspace, so stage
# all workspace manifests, then install only the runner's production deps.
COPY package.json bun.lock ./
COPY web/package.json ./web/package.json
COPY video/package.json ./video/package.json
COPY e2e/package.json ./e2e/package.json
COPY runner/package.json ./runner/package.json
RUN bun install --frozen-lockfile --production --filter @longhouse/runner

COPY runner/src ./runner/src
COPY runner/tsconfig.json ./runner/tsconfig.json

WORKDIR /app/runner

# Run the daemon
CMD ["bun", "run", "src/index.ts"]
