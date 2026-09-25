# Dependency preparation has network access, but receives only source manifests.
# Test execution uses a disposable container with --network none and no mounts.
ARG PLAYWRIGHT_VERSION=1.57.0
ARG TEST_MANIFEST_SHA
FROM rust:1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e AS rust
FROM oven/bun:1.2.20@sha256:78f46b81b82d767ee8d729411f6f95089a403c21f17c20a5789df00263d7c5b5 AS bun
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS python
FROM mcr.microsoft.com/playwright:v1.57.0-noble@sha256:3bed4b1a12f2338642f3d8cba28e291deef3c66bd4a964bbeb3e57bbff511dbd
ARG PLAYWRIGHT_VERSION
ARG TEST_MANIFEST_SHA
LABEL ai.longhouse.test-isolation.manifest-sha256=$TEST_MANIFEST_SHA
COPY --from=python /usr/local /usr/local
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:$PATH
RUN apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
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
# cargo-nextest runs each engine test in its own process (`make test-engine`).
# Pinned release binary, verified against the GitHub release asset digest.
ARG NEXTEST_VERSION=0.9.146
RUN set -eu; \
    arch="$(uname -m)"; \
    case "$arch" in \
      x86_64) sum=682c21b777c333e96fd532e114d3a5a894e0729ab88d94c0a9f20f8419695428 ;; \
      aarch64) sum=b2e33d7c72de7ade0ff7b3a948ac37516b24f8a836b7a8870c1f634a94be9de9 ;; \
      *) echo "no pinned cargo-nextest for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --retry 5 -o /tmp/cargo-nextest.tgz \
      "https://github.com/nextest-rs/nextest/releases/download/cargo-nextest-${NEXTEST_VERSION}/cargo-nextest-${NEXTEST_VERSION}-${arch}-unknown-linux-gnu.tar.gz"; \
    echo "$sum  /tmp/cargo-nextest.tgz" | sha256sum -c -; \
    tar -xzf /tmp/cargo-nextest.tgz -C /usr/local/cargo/bin cargo-nextest; \
    rm /tmp/cargo-nextest.tgz; \
    cargo nextest --version
# No credentials, user HOME, provider binaries, git configuration, or daemon socket.
ENV UV_OFFLINE=1 UV_NO_SYNC=1 CARGO_NET_OFFLINE=true
