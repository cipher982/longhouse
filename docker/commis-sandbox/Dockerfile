# Commis Sandbox Container
# Provides isolated execution environment for autonomous agent tasks.
#
# Security features:
# - Non-root user (agent, uid 1000)
# - Minimal base image
# - No secrets baked in (passed via environment)
#
# Usage:
#   docker build -t commis-sandbox:dev .
#   echo "What is 2+2?" | docker run --rm -i commis-sandbox:dev -b zai --json -

FROM python:3.12-slim

# Install system deps including Node.js for claude CLI
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    ca-certificates \
    gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
       | tee /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user FIRST
RUN useradd -m -s /bin/bash -u 1000 agent

# Install claude CLI globally (requires root)
RUN npm install -g @anthropic-ai/claude-code@latest

# Switch to agent user for remaining setup
USER agent
WORKDIR /home/agent

# Install uv (pinned version for reproducibility)
RUN curl -LsSf https://astral.sh/uv/0.5.0/install.sh | sh
ENV PATH="/home/agent/.local/bin:$PATH"

# Install hatch from GitHub (not on PyPI)
# hatch is the unified CLI for agent execution across backends
RUN uv tool install git+https://github.com/cipher982/hatch.git

# Set up environment for writable directories
ENV HOME=/home/agent
ENV XDG_CONFIG_HOME=/home/agent/.config
ENV XDG_CACHE_HOME=/home/agent/.cache

# Default working directory is /repo (mounted at runtime)
WORKDIR /repo

# Entrypoint is hatch CLI - accepts prompts via stdin with "-"
ENTRYPOINT ["hatch"]
