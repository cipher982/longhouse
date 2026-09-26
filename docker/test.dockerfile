# Dependency preparation has network access, but receives only source manifests.
# Test execution uses a disposable container with --network none and no mounts.
#
# Two images come from this file, both labelled with the same manifest hash:
#   full (the last stage, and the default `docker build` target): everything,
#     including Playwright's browsers and the Bun workspace node_modules.
#   lean (--target lean): Python venv, Rust toolchain with prebuilt engine
#     dependencies, and Bun, on the same Ubuntu base. About a third of full's
#     unpacked size. Hosted CI jobs that never touch a browser or node_modules
#     (backend, engine, provider contract, lifecycle) run on it, because a
#     hosted runner's image pull is bound by disk writes, not the network.
ARG PLAYWRIGHT_VERSION=1.57.0
ARG TEST_MANIFEST_SHA
FROM rust:1-bookworm@sha256:93ce27a88655056a51dbdd8f5f2d7ddc071c7b0070fb288a37b5a285fc83971e AS rust
FROM oven/bun:1.2.23@sha256:6ebf306367da43ad75c4d5119563e24de9b66372929ad4fa31546be053a16f74 AS bun
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS python
# The Playwright image below is built on Ubuntu noble; lean uses noble too.
FROM ubuntu:noble@sha256:008173c23f95b170204355c12626cb5a965d779a7e1283b09e9cffbb1bf33ca3 AS ubuntu

# --- Shared: system packages, Python, Bun -------------------------------------
FROM ubuntu AS toolchain
COPY --from=python /usr/local /usr/local
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
# The Playwright image sets these; lean must match so no test sees a
# different locale depending on which image a lane runs on. For the same
# reason lean adds the Playwright image's system data packages that tests can
# reach: tzdata (zoneinfo), netbase (/etc/services), media-types (mimetypes).
ENV LANG=C.UTF-8 LC_ALL=C.UTF-8
RUN export DEBIAN_FRONTEND=noninteractive && apt-get -o Acquire::Retries=5 update && apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
    make git build-essential pkg-config libssl-dev curl ca-certificates sqlite3 jq \
    tzdata netbase media-types openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && ln -s /usr/local/bin/bun /usr/local/bin/bunx \
    && ldconfig \
    && python3 -c 'import ssl, sqlite3, ctypes, lzma, bz2, zlib, uuid, readline, zoneinfo; zoneinfo.ZoneInfo("America/New_York")'
WORKDIR /work

# --- Rust toolchain, crate registry, prebuilt engine dependencies -------------
# Built once here and copied into both images.
FROM toolchain AS cargo-deps
COPY --from=rust /usr/local/cargo /usr/local/cargo
COPY --from=rust /usr/local/rustup /usr/local/rustup
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:$PATH
COPY engine/Cargo.toml engine/Cargo.lock engine/
COPY .cargo/config.toml .cargo/
RUN mkdir -p engine/src && touch engine/src/main.rs engine/src/longhouse.rs \
    && cargo fetch --manifest-path engine/Cargo.toml --locked \
    && rm -rf engine/src
# Self-contained: inputs are engine/Cargo.toml + Cargo.lock + .cargo/config.toml
# (already in the image hash, MANIFESTS in scripts/qa/test-isolation.py), the toolchain, and the
# registry fetched above; the only output is /work/.build/cargo-target, the
# directory scripts/build/cargo.py resolves for /work inside the guest.
# Every CI engine build (Engine tests, the lifecycle proof) uses
# CARGO_PROFILE=ci-test, so a guest compiles just the longhouse-engine crate
# instead of ~240 dependencies. Both dependency sets are built: `build --bins`
# and nextest's `test --no-run --bins --tests`, since dev-dependencies can
# unify features differently.
# The stub crate has no build.rs and empty bins; its own outputs and
# fingerprints are deleted so the real source (whose tarred mtimes may predate
# this layer) can never look fresh. Dependency fingerprints stay valid in the
# guest: registry packages are fingerprinted by version, not mtime, and paths,
# rustc, profile and features are identical; both images copy this stage to
# the same paths on the same Ubuntu release. Other profiles still build from
# scratch into the same target directory.
RUN set -eu; \
    target=/work/.build/cargo-target; \
    mkdir -p engine/src; \
    printf 'fn main() {}\n' > engine/src/main.rs; \
    cp engine/src/main.rs engine/src/longhouse.rs; \
    CARGO_TARGET_DIR="$target" cargo build --manifest-path engine/Cargo.toml \
      --locked --offline --profile ci-test --bins; \
    CARGO_TARGET_DIR="$target" cargo test --manifest-path engine/Cargo.toml \
      --locked --offline --profile ci-test --bins --tests --no-run; \
    rm -rf engine/src \
      "$target"/ci-test/longhouse* \
      "$target"/ci-test/deps/longhouse* \
      "$target"/ci-test/.fingerprint/longhouse-engine-*; \
    du -sh "$target"
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

# --- lean ---------------------------------------------------------------------
FROM toolchain AS lean
ARG TEST_MANIFEST_SHA
LABEL ai.longhouse.test-isolation.manifest-sha256=$TEST_MANIFEST_SHA
ENV UV_CACHE_DIR=/opt/uv-cache
COPY server/pyproject.toml server/uv.lock server/
# Precompiled bytecode: the venv otherwise ships zero .pyc files and every
# fresh container recompiles fastapi/pydantic/sqlalchemy on first import.
RUN cd server && UV_COMPILE_BYTECODE=1 uv sync --frozen --extra dev --no-install-project \
    && uv venv /opt/build-deps \
    && uv pip install --python /opt/build-deps/bin/python hatchling editables \
    && uv pip install --python .venv/bin/python hatchling editables \
    && rm -rf /opt/build-deps
COPY --from=cargo-deps /usr/local/cargo /usr/local/cargo
COPY --from=cargo-deps /usr/local/rustup /usr/local/rustup
COPY --from=cargo-deps /work/.build /work/.build
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:$PATH
# No credentials, user HOME, provider binaries, git configuration, or daemon socket.
ENV UV_OFFLINE=1 UV_NO_SYNC=1 CARGO_NET_OFFLINE=true

# --- full (default) -----------------------------------------------------------
FROM mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27
ARG PLAYWRIGHT_VERSION
ARG TEST_MANIFEST_SHA
LABEL ai.longhouse.test-isolation.manifest-sha256=$TEST_MANIFEST_SHA
COPY --from=python /usr/local /usr/local
COPY --from=bun /usr/local/bin/bun /usr/local/bin/bun
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
RUN bun install --frozen-lockfile \
    && node -e 'if (require("playwright/package.json").version !== process.env.PLAYWRIGHT_VERSION) throw new Error("Update PLAYWRIGHT_VERSION to match bun.lock")'
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY server/pyproject.toml server/uv.lock server/
RUN cd server && UV_COMPILE_BYTECODE=1 uv sync --frozen --extra dev --no-install-project \
    && uv venv /opt/build-deps \
    && uv pip install --python /opt/build-deps/bin/python hatchling editables \
    && uv pip install --python .venv/bin/python hatchling editables \
    && rm -rf /opt/build-deps
COPY --from=cargo-deps /usr/local/cargo /usr/local/cargo
COPY --from=cargo-deps /usr/local/rustup /usr/local/rustup
COPY --from=cargo-deps /work/.build /work/.build
ENV RUSTUP_HOME=/usr/local/rustup CARGO_HOME=/usr/local/cargo
ENV PATH=/usr/local/cargo/bin:$PATH
# No credentials, user HOME, provider binaries, git configuration, or daemon socket.
ENV UV_OFFLINE=1 UV_NO_SYNC=1 CARGO_NET_OFFLINE=true
