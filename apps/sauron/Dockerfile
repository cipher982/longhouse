# Sauron - Centralized Ops Scheduler
# Deployed as standalone service on clifford VPS
#
# Build from monorepo root: docker build -f apps/sauron/Dockerfile -t sauron .

FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      git \
      curl \
      wget \
      openssh-client \
      ffmpeg && \
    # GitHub CLI (for worklog commits via API)
    mkdir -p -m 755 /etc/apt/keyrings && \
    wget -qO- https://cli.github.com/packages/githubcli-archive-keyring.gpg | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null && \
    chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg && \
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" | tee /etc/apt/sources.list.d/github-cli.list > /dev/null && \
    apt-get update && \
    apt-get install -y --no-install-recommends gh && \
    rm -rf /var/lib/apt/lists/*

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy zerg package from monorepo (needed for zerg.jobs and zerg.config)
# The zerg package includes jobs/, config/, and other required modules
COPY apps/zerg/backend/zerg /app/zerg_pkg/zerg

# Create pyproject.toml for zerg package with deps for scheduler + builtin jobs
# Builtin jobs need: openai (backup_sentinel), pydantic, boto3 (email)
# External jobs (sauron-jobs) need: pymongo (llm-benchmarks), google-ads-api
RUN cat > /app/zerg_pkg/pyproject.toml << 'EOF'
[project]
name = "zerg"
version = "0.0.0"
requires-python = ">=3.12"
dependencies = [
    "apscheduler>=3.10.0,<4.0",
    "asyncpg>=0.30.0",
    "httpx>=0.28.0",
    "python-dotenv>=1.0.0",
    "openai>=1.0.0",
    "pydantic>=2.0.0",
    "boto3>=1.34.0",
    "pymongo>=4.6.0",
    "google-ads>=25.0.0",
    "tiktoken>=0.5.0",
    "minio>=7.2.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["zerg"]
EOF

# Copy sauron package
COPY apps/sauron/pyproject.toml apps/sauron/README.md ./
COPY apps/sauron/sauron ./sauron/

# Install zerg package first (provides zerg.jobs, zerg.config), then sauron
RUN uv pip install --system /app/zerg_pkg && \
    uv pip install --system .

# Create non-root user with writable home for jobs repo
# Jobs dir is in user home to avoid /opt permission issues with lock files
RUN useradd -m -u 1000 sauron && \
    mkdir -p /home/sauron/jobs /data && \
    chown -R sauron:sauron /app /home/sauron /data

# Entrypoint script (decodes SSH key from env var)
COPY apps/sauron/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

USER sauron

# Expose API port
EXPOSE 8080

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "sauron.main"]
