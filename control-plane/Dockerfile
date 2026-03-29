FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock README.md API.md /app/
RUN uv sync --frozen --no-install-project --no-dev

COPY control_plane /app/control_plane
COPY longhouse_shared /app/longhouse_shared
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8085

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD python -c "import json, sys, urllib.request; data = json.load(urllib.request.urlopen('http://127.0.0.1:8085/health', timeout=5)); sys.exit(0 if data.get('status') == 'ok' else 1)" || exit 1

CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8085"]
