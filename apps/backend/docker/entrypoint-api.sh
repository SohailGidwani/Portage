#!/usr/bin/env sh
# API entrypoint: apply domain migrations (Alembic owns the jobs table), then serve.
# The worker waits on this service's healthcheck, so by the time the worker starts the
# schema exists. LangGraph's checkpoint tables are created separately by the worker.
set -e

echo "[api] running alembic upgrade head ..."
alembic upgrade head

echo "[api] starting uvicorn ..."
# --forwarded-allow-ips "*": behind the Caddy reverse proxy (hosted, P8) the peer is
# Caddy's container IP, so uvicorn must trust X-Forwarded-Proto/For from it — otherwise
# url_for() builds http:// OAuth redirect URIs and the rate limiter keys on Caddy's IP.
# Safe because the api port is never published beyond loopback; only Caddy and the host
# itself can reach it.
exec uvicorn portage_agent.api:app --host 0.0.0.0 --port 8000 \
  --proxy-headers --forwarded-allow-ips "*"
