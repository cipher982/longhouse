# Dependency preparation has network access, but receives only source manifests.
# Test execution uses a disposable container with --network none and no mounts.
ARG PLAYWRIGHT_VERSION=1.57.0
FROM rust:1-bookworm AS rust
FROM oven/bun:1.2.20 AS bun
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS python
FROM mcr.microsoft.com/playwright:v${PLAYWRIGHT_VERSION}-noble
ARG PLAYWRIGHT_VERSION
COPY --from=python /usr/local /usr/local
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:$PATH
RUN apt-get update && apt-get install -y --no-install-recommends \
    make git build-essential pkg-config libssl-dev curl ca-certificates sqlite3 jq \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/bin/bun /usr/local/bin/bunx \
    && ln -s /ms-playwright /opt/playwright \
    && ldconfig
WORKDIR /work
ENV UV_CACHE_DIR=/opt/uv-cache BUN_INSTALL_CACHE_DIR=/opt/bun-cache
COPY package.json bun.lock ./
COPY web/package.json web/package.json
COPY e2e/package.json e2e/package.json
COPY runner/package.json runner/package.json
COPY video/package.json video/package.json
RUN bun install --frozen-lockfile \
    && node -e 'if (require("playwright/package.json").version !== process.env.PLAYWRIGHT_VERSION) throw new Error("Update PLAYWRIGHT_VERSION to match bun.lock")'
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY server/pyproject.toml server/uv.lock server/
RUN cd server && uv sync --frozen --extra dev --no-install-project \
    && uv venv /opt/build-deps \
    && uv pip install --python /opt/build-deps/bin/python hatchling editables \
    && uv pip install --python .venv/bin/python hatchling editables \
    && rm -rf /opt/build-deps
COPY engine/Cargo.toml engine/Cargo.lock engine/
RUN mkdir -p engine/src && touch engine/src/main.rs engine/src/longhouse.rs \
    && cargo fetch --manifest-path engine/Cargo.toml --locked \
    && rm -rf engine/src
RUN rustup component add rustfmt
# No credentials, user HOME, provider binaries, git configuration, or daemon socket.
ENV UV_OFFLINE=1 UV_NO_SYNC=1 CARGO_NET_OFFLINE=true
