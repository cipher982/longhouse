FROM oven/bun:1

# Build from runner directory:
#   docker build -t longhouse/runner:latest runner
# Or from repo root:
#   docker build -f runner/Dockerfile -t longhouse/runner:latest runner
WORKDIR /app

# Copy package files and install deps
COPY package.json ./
RUN bun install

# Copy source
COPY src ./src
COPY tsconfig.json ./

# Run the daemon
CMD ["bun", "run", "src/index.ts"]
