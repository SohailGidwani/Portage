# Backend image — shared by the `api` and `worker` services (command differs per service).
# uv-based, deps installed from the committed uv.lock (pins all versions).
#
# The worker also needs (the api ignores these, shared image for simplicity):
#   * git            — to clone repos in the Ingest node
#   * docker CLI      — to spawn the ephemeral sandbox (Docker-out-of-Docker)
#   * code-review-graph — installed ISOLATED via `uv tool` (its fastmcp/tree-sitter/networkx
#     deps never touch the project venv); the worker's `mcp` client launches `serve` (stdio).
FROM python:3.12-slim-bookworm

# Pinned uv (matches the host that generated uv.lock).
COPY --from=ghcr.io/astral-sh/uv:0.11.25 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_TOOL_BIN_DIR=/usr/local/bin \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# System deps: git (clone) + docker CLI (DooD). docker CLI = static client binary only.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && DOCKER_VER=27.3.1 ARCH="$(uname -m)" \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${ARCH}/docker-${DOCKER_VER}.tgz" \
    | tar xz --strip-components=1 -C /usr/local/bin docker/docker \
 && docker --version

# code-review-graph installed isolated (its own venv); console script on PATH via UV_TOOL_BIN_DIR.
RUN uv tool install "code-review-graph==2.3.6" \
 && code-review-graph --version

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
