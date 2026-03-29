FROM debian:bookworm-slim AS sqlite-builder

ARG SQLITE_VERSION=3510300
ARG SQLITE_SHA3=581215771b32ea4c4062e6fb9842c4aa43d0a7fb2b6670ff6fa4ebb807781204

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /tmp

RUN curl -fsSLO "https://sqlite.org/2026/sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && test "$(openssl dgst -sha3-256 "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" | awk '{print $2}')" = "${SQLITE_SHA3}" \
    && tar -xzf "sqlite-autoconf-${SQLITE_VERSION}.tar.gz" \
    && cd "sqlite-autoconf-${SQLITE_VERSION}" \
    && ./configure --prefix=/usr/local --disable-static --enable-threadsafe \
    && make -j"$(nproc)" \
    && make install DESTDIR=/sqlite-dist

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY --from=sqlite-builder /sqlite-dist/usr/local/ /usr/local/
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && ldconfig

COPY pyproject.toml uv.lock README.md API.md /app/
RUN uv sync --frozen --no-install-project --no-dev

COPY control_plane /app/control_plane
COPY longhouse_shared /app/longhouse_shared
COPY sitecustomize.py /app/sitecustomize.py
RUN uv sync --frozen --no-dev \
    && /app/.venv/bin/python -m ensurepip --default-pip \
    && /app/.venv/bin/pip install --no-cache-dir --no-binary pysqlite3 pysqlite3==0.6.0

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import json, sys, urllib.request; data = json.load(urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=5)); sys.exit(0 if data.get('status') == 'ok' else 1)" || exit 1

CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8085"]
