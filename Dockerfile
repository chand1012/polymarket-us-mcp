FROM ghcr.io/astral-sh/uv:0.11.21 AS uv
FROM python:3.14-slim

COPY --from=uv /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    HOST=0.0.0.0 \
    PORT=8000

COPY pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-dev --no-install-project
COPY main.py api_catalog.json ./
RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser
EXPOSE 8000
ENTRYPOINT ["/app/.venv/bin/python", "/app/main.py"]
