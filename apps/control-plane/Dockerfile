FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

COPY pyproject.toml uv.lock README.md API.md /app/
RUN uv sync --frozen --no-install-project --no-dev

COPY control_plane /app/control_plane
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8085

CMD ["uvicorn", "control_plane.main:app", "--host", "0.0.0.0", "--port", "8085"]
