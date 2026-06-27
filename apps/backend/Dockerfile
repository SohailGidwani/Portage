# Backend image — shared by the `api` and `worker` services (command differs per service).
# uv-based, deps installed from the committed uv.lock (pins all versions).
FROM python:3.12-slim-bookworm

# Pinned uv (matches the host that generated uv.lock).
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

# 1) Dependency layer (cached unless pyproject/lock change).
COPY pyproject.toml uv.lock .python-version ./
RUN uv sync --frozen --no-install-project --no-dev

# 2) Project source + the install of the project itself.
COPY . .
RUN uv sync --frozen --no-dev

# Default command is the API; the worker service overrides it in compose.
EXPOSE 8000
CMD ["uvicorn", "portage_agent.api:app", "--host", "0.0.0.0", "--port", "8000"]
