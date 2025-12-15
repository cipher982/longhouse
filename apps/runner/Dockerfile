FROM oven/bun:1

# Build from apps/runner directory:
#   docker build -t swarmlet/runner:latest apps/runner
# Or from repo root:
#   docker build -f apps/runner/Dockerfile -t swarmlet/runner:latest apps/runner
WORKDIR /app

# Copy package files and install deps
COPY package.json ./
RUN bun install

# Copy source
COPY src ./src
COPY tsconfig.json ./

# Run the daemon
CMD ["bun", "run", "src/index.ts"]
