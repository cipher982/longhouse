FROM oven/bun:1

WORKDIR /app

# Copy package files
COPY package.json bun.lock* ./

# Install dependencies
RUN bun install --frozen-lockfile

# Copy source code
COPY src ./src
COPY tsconfig.json ./

# Run the daemon
CMD ["bun", "run", "src/index.ts"]
