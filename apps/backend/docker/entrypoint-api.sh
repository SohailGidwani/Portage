#!/usr/bin/env sh
# API entrypoint: apply domain migrations (Alembic owns the jobs table), then serve.
# The worker waits on this service's healthcheck, so by the time the worker starts the
# schema exists. LangGraph's checkpoint tables are created separately by the worker.
set -e

echo "[api] running alembic upgrade head ..."
alembic upgrade head

echo "[api] starting uvicorn ..."
exec uvicorn portage_agent.api:app --host 0.0.0.0 --port 8000
