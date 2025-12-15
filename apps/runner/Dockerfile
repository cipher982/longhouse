FROM oven/bun:1

# This Dockerfile expects to be built with the *repo root* as the build context:
#   docker build -f apps/runner/Dockerfile -t swarmlet/runner:latest .
WORKDIR /repo

# Copy workspace manifests (single root lockfile policy)
COPY package.json bun.lock ./
COPY apps/runner/package.json apps/runner/package.json

# Install deps using the workspace lockfile
RUN bun install --frozen-lockfile

# Copy runner source
COPY apps/runner/src apps/runner/src
COPY apps/runner/tsconfig.json apps/runner/tsconfig.json

# Run the daemon from the workspace
CMD ["bun", "run", "--filter", "@swarmlet/runner", "start"]
