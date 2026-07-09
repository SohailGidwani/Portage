# Portage — P8 Deployment Runbook (AWS EC2, single box)

How this doc is organized:
- **Part A — Repo changes (Claude Code executes these).** Drop this file in the repo; when
  you point Claude Code at it, Part A is its task list. These are code/config edits that
  make the repo deploy-ready. Each has a Definition of Done.
- **Part B — Manual ops (you do these).** AWS console + SSH + DuckDNS + GitHub. Things
  Claude Code can't do for you.
- **Parts C–F — Reference:** Caddy explained, command cheatsheet, redeploy, failure modes.

Key facts baked in: single EC2 box runs the whole compose stack; public hostname is
**portage-agent.duckdns.org**; the platform model is **GPT-4o on Azure OpenAI** (via the
LiteLLM ladder); sandboxes run under **gVisor**.

---

## 0. Mental model (one box)

```
                 EC2 instance (one box)
   ┌───────────────────────────────────────────────┐
   │  caddy  :80/:443  ── TLS + reverse proxy        │  <- only thing public
   │    ├─ /        → frontend (Next.js)  :3000      │
   │    └─ /api/*   → api (FastAPI)       :8000      │
   │  worker (LangGraph) ── spawns ──► sandbox       │
   │  db (Postgres+pgvector)            (gVisor)     │
   └───────────────────────────────────────────────┘
```
Both backend and frontend run as containers here. "Public" = Caddy exposes them on 443.
Docker manages Postgres too; its data lives in a named volume that survives reboots.

---

## 1. Hostname & HTTPS (why not the bare IP)

Trusted TLS certs are never issued for bare IPs, and your P7 auth sets the refresh-token
cookie `Secure` — browsers won't send `Secure` cookies over plain HTTP, so `http://<ip>`
brings the stack up but silently breaks GitHub login. Free fix: the DuckDNS hostname
**portage-agent.duckdns.org** + Caddy auto-TLS. Two stages:
- **Stage 1 (smoke test on the IP, HTTP):** confirm the stack runs and a migration
  completes via API/CLI. Browser login won't fully work — expected.
- **Stage 2 (public demo):** DuckDNS + Caddy HTTPS → login works → link on LinkedIn.

---

# PART A — Repo changes (Claude Code task list)

> Make these edits in the repo, keep them minimal and idiomatic, and don't break the
> existing local `docker compose up`. Verify each DoD.

## A1. Add Caddy as a compose service + a Caddyfile
**Built, behind the compose profile `edge`** — local `docker compose up` never starts
Caddy; the hosted box opts in with `COMPOSE_PROFILES=edge` in `.env`:
```yaml
  caddy:
    image: caddy:2
    profiles: ["edge"]
    restart: unless-stopped
    ports: ["80:80", "443:443"]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
    depends_on: [api, frontend]
```
`volumes:` block carries `caddy_data:` and `caddy_config:`. `Caddyfile` in repo root:
```
portage-agent.duckdns.org {
    encode gzip
    handle_path /api/* {
        reverse_proxy api:8000
    }
    handle {
        reverse_proxy frontend:3000
    }
}
```
**DoD:** `docker compose config` validates; `caddy` service present with 80/443.

## A2. Make app services internal (only Caddy is public)
**Built as loopback bindings, not removed ports:** `api`/`frontend`/`db` publish to
`127.0.0.1` only. Off-box traffic can reach nothing but Caddy on 80/443 (the security
group blocks the rest anyway), while host-local `curl localhost:8000` still works — which
B11's smoke test and the `scripts/phase*_check.sh` DoD scripts run on this box require.
Caddy itself is behind the compose profile `edge` (set `COMPOSE_PROFILES=edge` in the
hosted `.env`), so a local dev `docker compose up` never starts it.
**DoD:** from another machine, `curl http://<ip>:8000` times out but Caddy on 80/443
reaches the API; on the box itself `curl localhost:8000/health` still returns 200.

## A3. FastAPI behind the stripped /api prefix
Caddy's `handle_path /api/*` strips `/api` before proxying, so external `/api/jobs` →
internal `/jobs`. **Built as env config:** `API_ROOT_PATH=/api` in the hosted `.env` sets
the app's `root_path` (empty locally). Routes are NOT prefixed in code (that would double
it) — root_path only fixes *generated* URLs: `/docs`, the OAuth `redirect_uri` from
`url_for`, and the refresh-cookie path (browser-visible scope becomes `/api/auth`).
Uvicorn runs with `--proxy-headers --forwarded-allow-ips "*"` so `url_for` behind Caddy
builds `https://` URLs (safe: the api port binds loopback-only, A2).
**DoD:** `https://…/api/health` returns 200 and `https://…/api/docs` renders.

## A4. Frontend API base URL
The Next.js app reads `NEXT_PUBLIC_API_URL`. NEXT_PUBLIC_* is inlined at **build** time,
so compose passes it as a build arg — changing it needs `docker compose up -d --build
frontend`. `.env.example` defaults to `http://localhost:8000` (local dev must keep
working); the hosted `.env` sets `NEXT_PUBLIC_API_URL=https://portage-agent.duckdns.org/api`.
**DoD:** built frontend makes requests to `/api/...` on the same origin.

## A5. Azure OpenAI wiring (platform model = GPT-4o on Azure)
**Already built** (Phase 2, verified on Azure GPT-4o) — the LiteLLM ladder reads:
```
AZURE_API_KEY=<azure-openai-key>
AZURE_API_BASE=https://<your-resource>.openai.azure.com
AZURE_API_VERSION=2024-08-01-preview        # use the version your deployment supports
LLM_DRIVER_MODEL=azure/<deployment-name>       # deployment name from Azure OpenAI Studio
LLM_ESCALATION_MODEL=azure/<deployment-name>   # same model; escalation = enriched retry
LLM_DRIVER_MODEL_LABEL=GPT-4o                  # public label; deployment id never leaves env
LLM_ESCALATION_MODEL_LABEL=GPT-4o
```
(There is no `LLM_QUICKFIX_MODEL` — the ladder has two tiers, driver + escalation.)
**DoD:** a migration job completes end-to-end using the Azure GPT-4o deployment; no raw
`OPENAI_API_KEY` is required for the platform model.

## A6. Worker requests the gVisor runtime for sandboxes
Where the worker creates sandbox containers (`sandbox/docker.py`), pass `runtime="runsc"`.
**Built as `SANDBOX_RUNTIME=runsc`** in the hosted `.env` (default empty = daemon default,
for local dev where runsc is absent). Installing runsc on the host (Part B) only makes it
*available*; the worker must *request* it — this flag is that request, on every
`docker run` the sandbox makes.
**DoD:** in an environment with runsc installed, sandbox containers run under gVisor
(`docker inspect` shows `Runtime: runsc`); with the flag unset, they run normally.

## A7. Resilience defaults
Add `restart: unless-stopped` to every service. Add Docker log rotation guidance to the
runbook/README (host `daemon.json`: `max-size`, `max-file`).
**DoD:** a host reboot brings the whole stack back automatically.

## A8. Tighten CORS
Since Caddy serves frontend and API on the same origin now, restrict FastAPI CORS to
`https://portage-agent.duckdns.org` (and `http://localhost:3000` for dev).
**DoD:** cross-origin requests from other origins are rejected.

## A9. A one-flag Stage-1 IP mode (optional but handy)
**Built as `docker-compose.stage1.yml`** (NOT `docker-compose.override.yml` — compose
auto-loads that filename into every plain `up`, which this must never be). It re-publishes
`api:8000` on all interfaces; Caddy simply isn't started (leave `COMPOSE_PROFILES=edge`
unset). Temporarily open port 8000 in the security group for the test, close it after.
**DoD:** `docker compose -f docker-compose.yml -f docker-compose.stage1.yml up -d`
exposes the API on `http://<elastic-ip>:8000` for testing.

---

# PART B — Manual ops (you, on AWS / DuckDNS / GitHub)

## B1. Launch EC2
Console → EC2 → Launch instance:
- Name `portage`; AMI **Ubuntu Server 24.04 LTS (x86_64)**; type **m7i-flex.large**
  (2 vCPU/8 GB — the memory match for t3.large if that's not offered in your
  account/region). Avoid `c7i-flex.large`: it's only 4 GB, which this runbook already
  flags as OOM risk under gVisor test runs. `t3.micro`/`t3.small` (1–2 GB) are too small
  to consider — the whole compose stack (Postgres + API + worker + frontend + gVisor
  sandboxes) needs headroom beyond just the app. Key pair `portage-key` (download `.pem`).
- **Security group inbound:** SSH 22 (Source: My IP), HTTP 80 (Anywhere), HTTPS 443
  (Anywhere). Nothing else. Port 80 must be open — Caddy needs it for the cert challenge.
- Storage: **30 GB gp3** (8 GB default fills up).

## B2. Elastic IP (stable address)
EC2 → Elastic IPs → Allocate → Associate with `portage`. Free while attached to a running
instance. Record this IP — DuckDNS points here.

## B3. SSH in
```bash
chmod 400 portage-key.pem
ssh -i portage-key.pem ubuntu@<ELASTIC_IP>
```

## B4. Install Docker Engine + Compose plugin (Ubuntu 24.04)
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER && newgrp docker
docker run --rm hello-world
```

## B5. Install gVisor (runsc)
```bash
sudo apt-get update && sudo apt-get install -y apt-transport-https ca-certificates curl gnupg
curl -fsSL https://gvisor.dev/archive.key | sudo gpg --dearmor -o /usr/share/keyrings/gvisor-archive-keyring.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/gvisor-archive-keyring.gpg] https://storage.googleapis.com/gvisor/releases release main" \
  | sudo tee /etc/apt/sources.list.d/gvisor.list > /dev/null
sudo apt-get update && sudo apt-get install -y runsc
sudo runsc install && sudo systemctl reload docker
docker run --rm --runtime=runsc hello-world      # boots the gVisor kernel = success
```

## B6. Clone the repo (public)
```bash
cd ~ && git clone https://github.com/SohailGidwani/Portage.git && cd Portage
```

## B7. Fill `.env`
```bash
cp .env.example .env && nano .env
```
Set (the "Hosted deployment" block at the bottom of `.env.example` lists exactly these):
- `COMPOSE_PROFILES=edge` (starts Caddy) and `API_ROOT_PATH=/api`
- `NEXT_PUBLIC_API_URL=https://portage-agent.duckdns.org/api`,
  `FRONTEND_ORIGIN=https://portage-agent.duckdns.org`,
  `CORS_ORIGINS=https://portage-agent.duckdns.org`
- `SANDBOX_RUNTIME=runsc` (A6/B5)
- Azure OpenAI creds (A5 — `AZURE_API_KEY`, `AZURE_API_BASE`, `AZURE_API_VERSION`,
  `LLM_*_MODEL=azure/<deployment>`, the `*_LABEL`s)
- `POSTGRES_PASSWORD` (strong); `AUTH_MODE=github` + GitHub OAuth client id/secret (B9);
  `JWT_SECRET` (≥32 random bytes: `openssl rand -hex 32`)
- Demo limits: `GLOBAL_DAILY_SPEND_CAP_USD` (kill switch), `JOB_COST_CEILING_USD`,
  `MAX_CONCURRENT_JOBS_PER_USER`, `MAX_JOBS_PER_DAY_PER_USER`

Never commit `.env`.

## B8. DuckDNS → Elastic IP
duckdns.org → sign in → your subdomain **portage-agent** → set its IP to your Elastic IP.
(With an Elastic IP the address never changes, so no updater cron needed.)
Verify: `dig +short portage-agent.duckdns.org` → your Elastic IP.

## B9. GitHub OAuth app
GitHub → Settings → Developer settings → OAuth Apps → New:
- Homepage: `https://portage-agent.duckdns.org`
- Callback: `https://portage-agent.duckdns.org/api/auth/github/callback` (match your real
  FastAPI route)
- Copy Client ID + new Client Secret → into `.env`.

## B10. Bring it up + migrate
```bash
docker compose --profile tools build sandbox     # build the sandbox image
docker compose up -d                             # start everything in the background
docker compose ps                                # all healthy?
docker compose logs -f api                        # watch boot; Ctrl-C stops watching only
docker compose exec api alembic upgrade head      # if migrations aren't auto-run
```
Caddy fetches the Let's Encrypt cert on first start (needs port 80 + DNS resolving). To
rehearse without hitting rate limits, first point Caddy at LE staging
(`acme_ca https://acme-staging-v02.api.letsencrypt.org/directory`), confirm, then remove it.

## B11. Smoke test
```bash
curl -s https://portage-agent.duckdns.org/api/health
curl -X POST https://portage-agent.duckdns.org/api/jobs \
  -H 'content-type: application/json' \
  -d '{"repo_url":"/fixtures/flask_app","migration_recipe":"flask_to_fastapi"}'
bash scripts/phase2_check.sh && bash scripts/phase3_check.sh
```
Caveat: with `AUTH_MODE=github` the anonymous `POST /jobs` above returns **401 — that's
the auth working**, not a failure. The DoD scripts likewise assume `AUTH_MODE=disabled`;
run them during the Stage-1 smoke (auth off), not here. For an authed curl job: sign in
once in the browser, then grab an access token and mint a `pk_` key (no UI yet — two
calls):
```bash
# in the browser devtools console, copy access_token from:  POST /api/auth/refresh
curl -X POST https://portage-agent.duckdns.org/api/auth/keys \
  -H "authorization: Bearer <access-jwt>" -H 'content-type: application/json' \
  -d '{"name":"ops"}'          # -> returns pk_... exactly once
curl -X POST https://portage-agent.duckdns.org/api/jobs \
  -H 'authorization: Bearer pk_...' -H 'content-type: application/json' \
  -d '{"repo_url":"/fixtures/flask_app","migration_recipe":"flask_to_fastapi"}'
```
Then open the dashboard in a browser, sign in with GitHub, watch a run.

## B12. Ops hardening (before posting the link)
- **Backups:** nightly `pg_dump` (cron) → gzip → `aws s3 cp` to a bucket.
- **Uptime:** UptimeRobot (free) on `/api/health`.
- **Billing alarm:** AWS Budgets at 50% / 80% of credits.
- **Kill switch + quotas:** confirm the P7 daily Azure-spend cap + per-user quotas are live
  so a LinkedIn spike degrades to a waitlist, not a drained key.
- **Log rotation:** `/etc/docker/daemon.json` → `{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}` then `sudo systemctl restart docker`.

---

# PART C — Caddy, explained

- **What it is:** a small open-source web server (single Go binary) famous for automatic
  HTTPS. We run it as a container (`image: caddy:2`) — nothing to install by hand; `docker
  compose up` pulls it. The only file you author is the `Caddyfile`.
- **Job 1 — reverse proxy:** it's the only process on 80/443. It inspects each request and
  forwards it internally: `/api/*` → FastAPI, everything else → Next.js. Outsiders never
  touch the app containers directly.
- **Job 2 — automatic TLS:** on first boot it proves control of
  `portage-agent.duckdns.org` to Let's Encrypt (HTTP challenge on port 80), installs the
  cert, and auto-renews forever. No certbot, no cron, no cert files.
- **Why not nginx:** nginx needs certbot + a renewal cron + manual TLS blocks; Caddy does
  the same in ~4 lines. Less to get wrong for a solo demo.

---

# PART D — Command cheatsheet

- `docker compose up -d` — start all services in the **background** (shell returns
  immediately; you can test freely).
- `docker compose ps` — status of each service.
- `docker compose logs api` — snapshot of one service's logs; add `-f` to stream live
  (Ctrl-C stops streaming, not the container).
- `docker compose exec api <cmd>` — run a command inside the running api container (e.g.
  `alembic upgrade head`, `bash`).
- `docker compose restart worker` — restart one service.
- `docker compose down` — stop + remove containers; **DB data in the `pgdata` volume
  survives.**
- `docker compose down -v` — also delete volumes → **wipes the database.** Careful.
- `docker volume ls` / `docker system df` — inspect volumes / disk usage.

**On the DB:** Postgres is the `db` service (`pgvector/pgvector:pg16`). Docker runs it; you
never install Postgres on the host. Data persists in the `pgdata` named volume across
reboots and `down`. Back it up with `pg_dump` — a volume on one box is not a backup.

---

# PART E — Redeploy (day-to-day)

```bash
cd ~/Portage
git pull
docker compose --profile tools build sandbox      # if the sandbox image changed
docker compose up -d --build                       # rebuild changed services
docker compose exec api alembic upgrade head       # if new migrations landed
```

---

# PART F — Common failure modes

- **Cert won't issue:** port 80 closed, or DNS not resolving yet (`dig
  portage-agent.duckdns.org`). Test with LE staging first.
- **Login loops / cookie not set:** you're on `http`/bare IP. Move to Stage 2 (HTTPS).
- **OAuth redirect mismatch:** the GitHub callback URL must exactly equal the FastAPI route
  including `https` and `/api`.
- **Azure model errors:** wrong `AZURE_API_VERSION` for the deployment, or model string
  isn't `azure/<deployment-name>`. Confirm the deployment name in Azure OpenAI Studio.
- **Sandbox fails only under gVisor:** a blocked/edge syscall; check `runsc` logs, confirm
  the worker passes `runtime=runsc`, and that the sandbox image doesn't need it.
- **OOM mid-migration:** go `t3.xlarge`, or lower sandbox concurrency.
- **Disk full:** enable log rotation (B12), `docker system prune -af` (careful), grow EBS.
