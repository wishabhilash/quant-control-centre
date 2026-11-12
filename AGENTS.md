# AGENTS.md

Operating guide for coding agents working in this repository.
**Quant Strategy Control Centre** — a web-based control centre to manage, edit,
deploy, schedule and monitor algorithmic trading strategies.

> This project's code, comments and tests reference a "Technical Specification
> v1.2" by section number (spec §4, §7, §10, §13, §14, §16 …). When a comment or
> test cites a section, treat it as the source of intent — preserve that behaviour
> and keep the section citation when you edit the surrounding code.

---

## 1. Golden rules (do not break these)

These invariants are load-bearing. Violating any one silently corrupts the
platform or a live trading account.

1. **S3 is the single source of truth.** Each strategy is *one JSON document* at
   `strategies/{strategy_id}.json`. All project source files are
   **Base64-encoded** inside `code.files` (spec §5, §8). There is **no
   database, no GitHub, no git-based versioning, no execution-history store** —
   do not introduce one.
2. **Monaco defines the project; the platform is agnostic** about filenames,
   file types and directory structure. Never assume a fixed layout or entry-point
   name inside a strategy project.
3. **Independent Python venv per strategy.** Each strategy runs with its *own*
   interpreter at `{strategy_dir}/.venv/bin/python`. Never run a strategy from the
   platform's own venv.
4. **One bounded `strategy.log` per strategy**, streamed live over WebSocket
   (spec §13). Never add a second log sink or let a strategy write its own.
5. **Deployment is a synchronisation, not a copy.** The deployed project must
   become an exact mirror of the saved Monaco project, *excluding*
   platform-managed resources (`.venv/`, `strategy.log`, `strategy.pid`).
6. **Strategy code is untrusted input.** The platform enforces safe ZIP
   extraction, argument-array execution (no shell), a non-root user and
   restricted permissions. A venv + non-root user **is not a sandbox** — do not
   relax any of the ZIP/path checks (see §6).
7. **Secrets are never stored in a strategy document** (spec §16). Broker
   credentials come from AWS Secrets Manager / SSM; platform AWS credentials come
   from the **EC2 IAM role** (no static keys in production).
8. **Live strategies do not auto-resume after EC2 loss** (spec §18, §22). Broker
   reconciliation and explicit authorisation are required. Preserve this.

---

## 2. Repository layout

```
platform/
  backend/              FastAPI app + services + storage + runner + tests
    app/
      main.py            create_app(); wires routers, CORS, static frontend
      api/               strategies · code · deploy · schedules · logs · system · deps
      core/              config (Settings) · logging · paths · security
      models/            common · strategy · system   (Pydantic v2 DTOs)
      services/          code · deploy · log · process · scheduler · strategy
      storage/           s3 (Boto3 wrapper) · strategy_store (JSON doc store)
    scripts/runner.py    generic cron/manual entry point (spec §10, §11)
    tests/               offline pytest suite (in-memory / fake S3)
    requirements.txt  pytest.ini
  frontend/             React + TypeScript + Vite + Tailwind + Monaco + zustand
    src/
      api/client.ts      typed fetch client (base /api/v1)
      components/        Dashboard · FileExplorer · MonacoEditor · StrategyWorkspace · Sidebar
      components/tabs/   CodeTab · ConfigTab · LogsTab · OverviewTab · SchedulesTab
      pages/SystemSettings.tsx
      lib/project.ts  store.ts (zustand)  types/strategy.ts
runtime/                platform runtime: locks/ · state/ · platform.cron
strategies/             per-strategy projects, .venv, strategy.log (created at runtime)
scripts/ensure_localstack_bucket.py
Makefile
.env.development  .env.production.example
```

On EC2 the filesystem mirrors spec §4 `/opt/quant/` (`PLATFORM_ROOT`, default
`/opt/quant`), parameterised by `PLATFORM_ROOT`. The `runtime/` and `strategies/`
trees at the repo root are the *local* equivalents.

---

## 3. Getting it running (local)

Requires Node ≥ 18, Python ≥ 3.11 (3.12 recommended) and Docker (LocalStack).
The scheduler is **dry-run by default** (`SCHEDULER_DRY_RUN=true`), so local work
never touches your machine's crontab.

```bash
make localstack          # start LocalStack (:4566) + create bucket quant-platform-dev
# `make backend` auto-creates platform/backend/.env (via `make env`);
# or: cp .env.development platform/backend/.env

cd platform/backend
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
make backend             # uvicorn :8000  (RELOAD=0 ./venv/bin/uvicorn app.main:app --reload to disable reload)

cd platform/frontend
npm install
make frontend            # vite :5173, proxies /api and /system -> :8000
# open http://localhost:5173
```

API docs: `http://localhost:8000/api/v1/docs`. Health: `/api/v1/system/health`.

### Make targets

| Target | Purpose |
|---|---|
| `make localstack` / `make localstack-down` | Start/stop LocalStack + create dev bucket (volume preserved) |
| `make env` | Ensure `platform/backend/.env` exists (from `.env.development`; idempotent) |
| `make backend` / `make frontend` | Run the services |
| `make build` | Build frontend → `platform/frontend/dist` (isolated `.npm-cache`) |
| `make lock` | Regenerate `package-lock.json` (commit it for reproducible `npm ci`) |
| `make test` | Backend pytest suite |
| `make lint` | Frontend typecheck (`tsc --noEmit`) |
| `make clean` | Remove caches + build artefacts |
| `make help` | List all targets |

---

## 4. Architecture

```
Frontend (React/TS/Vite/Monaco) ──REST/WS──▶ Backend (FastAPI/Pydantic/Boto3)
   services · api · storage │ one JSON doc / strategy ▼
                    Amazon S3 / LocalStack : strategies/*.json

EC2 runtime:
  /opt/quant/platform/backend     /opt/quant/platform/frontend
  /opt/quant/strategies/STR-00x/{ project files, .venv, strategy.log }
  /opt/quant/runtime/{ locks, state, platform.cron }
```

### Backend
- **App assembly**: `app/main.py::create_app()` builds the FastAPI app and mounts
  every router under `settings.api_base_path` (default `/api/v1`). The module
  exposes `app = create_app()`. Serve the built frontend via `StaticFiles` when
  `platform/frontend/dist` exists.
- **Config**: `app/core/config.py::Settings` is **100% environment-driven** so the
  same Boto3 abstraction works against LocalStack (dev) and AWS S3 (prod).
  `get_settings()` is `@lru_cache`-d — it is a process singleton.
- **Storage**: `S3Storage` (thin Boto3 wrapper) + `StrategyStore` (JSON document
  store). Object key = `strategies/{strategy_id}.json`; writes use **AES256
  server-side encryption** and **ETag conditional writes** for idempotency /
  no-silent-overwrite.
- **Dependency injection**: service singletons live in `app/api/deps.py`
  (`@lru_cache`). Services accept `settings: Settings | None = None` and fall back
  to `get_settings()`.
- **Routers**: `strategies` (CRUD), `code` (get/save/ZIP upload), `deploy`
  (deploy/start/stop/restart/status — *start/stop/restart are cron* operations:
  they enable/disable the strategy's schedules; the platform never launches or
  kills a process itself), `schedules` (CRUD + enable/disable),
  `logs` (tail/download/WebSocket stream), `system` (health/storage/scheduler).
  Every admin router is wrapped in `Depends(require_auth)`.
- **Runner** (`scripts/runner.py`): generic, invoked by Linux cron (the
  platform's *only* way to run a strategy), keyed by a unique `--schedule-id`.
  Verifies enabled/deployed/schedule state, acquires the OS lock, launches the
  strategy's own venv interpreter with a generated runtime config, and enforces
  the timeout.

### Frontend
- **Stack**: React 18 + TypeScript 5.7 + Vite 6 + Tailwind 3 + Monaco
  (`@monaco-editor/react`) + zustand 4.
- `src/api/client.ts` — single typed fetch client; base `/api/v1`; Bearer token
  from `localStorage`; throws `ApiError`. **Responses are validated at runtime**,
  not just at compile time.
- `src/types/strategy.ts` mirrors the backend Pydantic models — keep the two in
  sync when a model field changes.
- `src/store.ts` — zustand store (navigation + current strategy).
- Dev server (:5173) proxies `/api` and `/system` to `API_PROXY_TARGET`
  (default `http://localhost:8000`); in prod the built frontend is served by the
  same host as the API.
- **New Strategy** opens `src/components/StrategyWorkspace.tsx`, a full-page IDE
  workspace (not a modal): left = Monaco file explorer, centre = Monaco editor with
  open-file tabs + unsaved-change dots, right = per-schedule cron panel. The header
  shows strategy name, a configurable Python entry point, and save/deploy status;
  a collapsible Configuration section holds initial capital, runtime and parameters.
  **Save** persists the whole tree + configuration + schedules into the strategy's
  single S3 JSON document (`strategiesApi.create/update`, `codeApi.save`, `schedulesApi.*`);
  **Deploy** = save-then-`deployApi.deploy` (deploys every saved file, preserving
  structure). **Cancel** returns to the strategy list, prompting “Discard changes / Keep
  editing” only when unsaved changes exist, and never saves or deploys. `CreateStrategyModal`
  is removed; `+ New strategy` navigates to the `new-strategy` route.

### Data model (key identifiers & enums)
- **Strategy ID**: `STR-NNN` (3-digit, zero-padded, sequential).
- **Schedule ID**: `SCH-NNN`.
- **Deployment status**: `not_deployed | deployed | failed`.
- **Runtime states**: `not_deployed | stopped | running | failed | disabled`.
- **Cron**: 5- or 6-field expression; default timezone `Asia/Kolkata`;
  default region `ap-south-1`.
- **Runtime config** is written to `{strategy_dir}/.venv/share/runtime_config.json`
  — it is **not** part of the user's Monaco project and must never be persisted
  into the S3 document.

---

## 5. Conventions

### Python
- Start every module with `from __future__ import annotations` and a module
  docstring (often a spec section reference). Keep both.
- **Type hints throughout.** Pydantic **v2** for models and `Settings`.
- Each service owns a dedicated exception (`CodeError`, `DeployError`,
  `ProcessServiceError`, `StrategyServiceError`, `S3Error`) and the **API layer**
  maps these to HTTP status codes. Services raise domain errors; they do not raise
  `HTTPException`.
- Services are constructed with `settings=None` / `store=None` fallbacks so tests
  can inject fixtures.
- Use the platform's logger (`app/core/logging.py::get_logger`), not `print`
  (except the runner, which logs to stdout for cron capture).
- Prefer argument-array `subprocess` calls over `shell=True` (no interpolation).

### API
- Base path `/api/v1`. Keep routes under the router `prefix`
  (`/strategies`, `/system`). Conditional writes carry an ETag
  (`if_match_etag`).
- Interactive docs: `/api/v1/docs`.

### Frontend
- TypeScript strict; run `make lint` (`tsc --noEmit`) before committing.
- Keep `src/types/strategy.ts` aligned with `app/models/strategy.py`.
- Build output goes to `platform/frontend/dist`; `make lock` to refresh
  `package-lock.json`.

---

## 6. Security & safety (critical for a trading platform)

- **Safe ZIP extraction** (`code_service.import_zip` + `core/paths.py`) rejects:
  path traversal (`..`), absolute paths, symlinks (checked via `external_attr`
  unix mode), non-regular files, reserved-resource collisions, and exceeds
  `max_upload_bytes` (50 MB) / `max_upload_files` (5000) / `max_single_file_bytes`
  (10 MB). **Do not weaken any of these.**
- **Reserved resources** (`core/paths.py::RESERVED_TOP_LEVEL`): `.venv`,
  `strategy.log`, `strategy.pid` — a user project can never overwrite or delete
  them. Preserve `assert_safe_path` / `is_reserved` usage everywhere paths enter.
- **Execution**: dedicated non-root user, argument-array commands, per-strategy
  venv, OS-level execution lock (`runtime/locks/{id}.lock`) preventing duplicate
  runs, bounded `strategy.log` (10 MB), enforced run timeout
  (`SIGTERM` → grace → `SIGKILL` on the process group).
- **Auth** (`core/security.py`): `require_auth` enforces a Bearer token when
  `AUTH_ENABLED`; `ensure_production_https` rejects non-HTTPS in production.
  Production uses the EC2 IAM role — **never hard-code AWS credentials.**
- **Kill switch** (`process_service.set_kill_switch`): blocks new order
  submissions via a local guard flag; it never auto-cancels broker orders.

---

## 7. Testing

- Backend: `make test` (offline; `tests/conftest.py` provides `FakeS3`, a
  temp-dir `settings`, `store` and `strategy_service`). Tests run with
  `ENVIRONMENT=test` and `SCHEDULER_DRY_RUN=true`.
- Add coverage per spec §19: storage, editor/project, deployment, scheduling,
  runtime, logging and **security behaviour** (ZIP/path rejection, ETag
  conditional write, lock, timeout, kill switch).
- Tests never touch real AWS — keep them on the in-memory / fake S3 path.

---

## 8. Deployment / scheduling flow (how pieces fit)

- **Deploy** (`deploy_service.deploy`, spec §10): block if running (unless
  `wait_if_running`), validate entrypoint + paths, compute source hash, stage into
  a temp dir, `py_compile` every `.py`, create/refresh the venv, install
  dependencies, **smoke-test import the entry point**, then sync into the live
  dir, reconcile cron, and mark `deployed` with the source hash. **Staging
  guarantees the live project is untouched on failure.**
- **Schedule reconcile** (`scheduler_service.reconcile`): rewrites the
  platform-owned crontab file from enabled schedules; only installs into the
  system crontab when not in dry-run. Never clobbers unrelated system cron.
- **Start/stop are cron operations** (`deploy.py` → `SchedulerService`, spec
  §14): *start* enables the strategy's schedules and *stop* disables them —
  both reconcile the platform crontab. The platform never launches or kills a
  process itself.
- **Run trigger is cron-only.** The **runner** (`scripts/runner.py`, fired by
  `cron`) is the only thing that launches and (on timeout) kills a run:
  lock → launch own venv interpreter → write state JSON → enforce timeout.
  `process_service.runtime_status` still derives `running` from a live PID, and
  `set_kill_switch` blocks new orders (it never auto-cancels broker orders).

---

## 9. Checklist before finishing a change

- [ ] Preserve the golden rules in §1 (esp. S3-as-truth, untrusted-code checks).
- [ ] `make test` passes (offline); new security path has a test.
- [ ] `make lint` passes (frontend) and edited Python is type-consistent.
- [ ] If a model field changed: update **both** `app/models/strategy.py` and
      `src/types/strategy.ts`.
- [ ] No static AWS credentials, no secret in a strategy document, no new log
      sink, no new data store.
- [ ] Strategy start/stop/restart reconcile the crontab — no direct process
      launch or kill; the cron-fired runner is the only trigger.
- [ ] Keep spec-section citations and module docstrings when editing.
- [ ] New Strategy uses the full-page `StrategyWorkspace` IDE, not a modal.

---

## 10. Gotchas

- **`get_settings()` is cached** — it won't pick up env changes within a running
  process; restart uvicorn (or reload) after editing `.env`.
- **Scheduler dry-run** is the local default; a "successful" local reconcile only
  writes `runtime/platform.cron`, it does *not* install into your crontab.
- **Stop ≠ kill.** `stop`/`disable` only remove the strategy's cron lines; a run
  already in progress (fired by a prior tick) finishes on its own — it is never
  auto-killed and never auto-closes broker positions (spec §18/§22). To halt an
  in-flight run, use the kill switch or restart.
- **`platform/backend/scripts/runner.py`** hard-codes `/opt/quant/...` paths
  (spec §4 EC2 layout) — correct on EC2, irrelevant to local dry-run.
- **Binary ZIP assets** are stored as `"\x00BINARY\x00" + base64(...)` — a
  deliberate marker; do not "clean it up" into plain base64.
- **`.npm-cache/`** exists so a root-owned `~/.npm` (a known npm bug) can't break
  the build; build via `make build`, not a bare `npm run build`.
- **`.env` must live in `platform/backend/`** (the CWD of `make backend`); `Settings` loads `.env` relative to CWD, so a repo-root `.env` is ignored. `make backend` now auto-provisions it via the `env` target, but if the backend is started by hand, run `make env` first. Without it the backend falls back to real AWS S3 with the placeholder `localstack`/`testpass` creds and fails with `InvalidAccessKeyId` (not a LocalStack problem).
- Directory is spelled **"centre"** (British) on disk; the product name uses the
  same spelling. Keep it consistent.
