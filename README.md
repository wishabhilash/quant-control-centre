# Quant Strategy Control Centre

A web-based control centre to **manage, edit, deploy, schedule and monitor**
algorithmic trading strategies. It acts as a lightweight IDE, deployment manager,
scheduler and runtime monitor.

> **S3 is the persistent source of truth.** Each strategy is one JSON document in
> S3. **Monaco defines the project** — the platform is agnostic about filenames,
> file types and directory structure. No database, no GitHub, no versioning, no
> execution-history store.

---

## Architecture

```
┌─────────────────────────────┐        ┌───────────────────────────┐
│  Frontend (React + TS + Vite)│  REST  │  Backend (FastAPI)         │
│  Tailwind · Monaco Editor    │◀──────▶│  Pydantic · Boto3          │
│  Dashboard · Strategy IDE    │  WS    │  services · api · storage   │
└─────────────────────────────┘        └───────────┬───────────────┘
                                                     │ one JSON doc / strategy
                                                     ▼
                                            ┌────────────────────┐
                                            │  Amazon S3 / Local │
                                            │  strategies/*.json │
                                            └────────────────────┘

  EC2 runtime:
    /opt/quant/platform/backend   /opt/quant/platform/frontend
    /opt/quant/strategies/STR-00x/{ project files, .venv, strategy.log }
    /opt/quant/runtime/{ locks, state, platform.cron }
```

* **One JSON document per strategy** in `strategies/{strategy_id}.json`, with all
  source files **Base64-encoded** in `code.files` (spec §5, §8).
* **Independent Python venv per strategy**; **multiple independent Linux cron
  schedules per strategy**, each invoking the generic `runner.py` with a unique
  `--schedule-id` (spec §10, §11).
* **Exactly one bounded `strategy.log` per strategy**, streamed live over a
  WebSocket to the dashboard (spec §13).

See the [Technical Specification v1.2] for the full requirements.

---

## Directory layout (this repo)

```
platform/
  backend/          FastAPI app, services, storage abstraction, runner, tests
    app/            api · services · models · storage · core
    scripts/runner.py   generic cron/manual runner
    tests/          offline pytest suite (in-memory S3)
  frontend/         React + TypeScript + Vite + Tailwind + Monaco
runtime/            platform runtime (locks, state, platform.cron)
strategies/         per-strategy projects, .venv and strategy.log (created at runtime)
```

Runtime layout on EC2 mirrors the spec §4 `/opt/quant/` layout, parameterised by
`PLATFORM_ROOT`.

---

## Quick start (local development)

Requires Node ≥ 18, Python ≥ 3.11 (3.12 recommended) and Docker (for LocalStack).

```bash
# 1. Storage — start LocalStack (S3-compatible) and create the dev bucket
make localstack     # docker run localstack/localstack on :4566 + create quant-platform-dev
# (Optional) `make backend` auto-creates platform/backend/.env from
# .env.development via `make env`; or create it by hand:
#   cp .env.development platform/backend/.env   # LocalStack endpoint :4566

# 2. Backend
cd platform/backend
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
make backend               # uvicorn :8000 (or: RELOAD=1 ./venv/bin/uvicorn app.main:app --reload)

# 3. Frontend
cd platform/frontend
npm install
make frontend              # vite :5173, proxies /api -> :8000

# open http://localhost:5173
```

The scheduler runs in **dry-run mode by default** (`SCHEDULER_DRY_RUN=true`): it
writes a platform-owned crontab file but does **not** install it into the system
crontab, so local testing never touches your Mac's crontab (spec §17).

### Useful targets

| Target | Purpose |
|---|---|
| `make localstack` | Start LocalStack and create the dev bucket (also `localstack-down`) |
| `make env` | Ensure `platform/backend/.env` exists (from `.env.development`; idempotent) |
| `make backend` / `make frontend` | Run the services |
| `make build` | Build the frontend into `platform/frontend/dist` |
| `make test` | Run the backend pytest suite |
| `make lint` | Typecheck the frontend (`tsc --noEmit`) |
| `make clean` | Remove caches and build artefacts |

---

## Configuration

Configuration is 100% environment-driven so the same Boto3 storage abstraction
works in dev (LocalStack) and prod (AWS S3). See `.env.development` and
`.env.production.example`. Key variables:

| Variable | Meaning |
|---|---|
| `ENVIRONMENT` | `development` / `production` / `local` / `test` |
| `S3_BUCKET`, `S3_ENDPOINT_URL`, `AWS_REGION` | S3 / LocalStack target |
| `S3_ADDRESSING_STYLE` | `auto` (path-style for a custom endpoint / LocalStack) |
| `AUTH_ENABLED`, `DEV_API_TOKEN`, `HTTPS_REQUIRED` | security (prod) |
| `SCHEDULER_DRY_RUN` | install crontab (prod) vs. dry-run (dev) |
| `PLATFORM_ROOT` | filesystem root (default `/opt/quant`) |

In production, AWS credentials come from the **EC2 IAM role** — never set static
keys. Secrets for strategies are supplied separately via AWS Secrets Manager / SSM
and are **never** stored in strategy documents (spec §16).

---

## API

Base path `/api/v1`. See spec §14 for the full surface (strategy CRUD, code,
deployment, schedules, logs incl. WebSocket, system health). Interactive docs are
available at `http://localhost:8000/api/v1/docs`.

## Security model

Strategy code is **untrusted input**. The platform enforces safe ZIP extraction
(rejects path traversal, absolute paths, symlinks, reserved-path collisions and
exceeds file/size limits), argument-array command execution (no shell
interpolation), a dedicated non-root execution user, restricted filesystem
permissions and dashboard authentication with CSRF protection in production
(spec §16). A venv + non-root user alone are not a sandbox.

## Disaster recovery

S3 is the source of truth. After EC2 loss: provision a replacement, redeploy,
point it at the existing bucket, and the platform reconstructs strategy
directories, venvs and cron schedules from S3. **Live strategies do not resume
automatically** — broker-state reconciliation and explicit authorisation are
required (spec §18, §22).

## Testing

`make test` runs the backend suite (offline, in-memory S3). Coverage targets
storage, editor/project, deployment, scheduling, runtime, logging and security
behaviour per spec §19.
