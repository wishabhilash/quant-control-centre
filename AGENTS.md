# AGENTS.md

Operating guide for coding agents working in this repository.
**Quant Strategy Control Centre** — a web-based control centre to manage, edit,
deploy, schedule and monitor algorithmic trading strategies.

> This project's code, comments and tests reference a "Technical Specification
> v1.2" by section number (spec §4, §7, §10, §13, §14, §16 …). When a comment or
> test cites a section, treat it as the source of intent — preserve that behaviour
> and keep the section citation when you edit the surrounding code.
> The frontend strategy workspace UI is governed by specification amendment
> v1.3 (one full-page, 4-tab create+edit workspace). The v1.2 section
> references above still hold for everything else.

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
   file types and directory structure. The *one* fixed point is the entry
   point: every strategy ships a `main.py` that the runner invokes; it is not
   user-configurable (a schedule's `command` supplies its arguments). Beyond that
   entry point, never assume a fixed layout inside a strategy project.
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
      components/        Dashboard · FileExplorer · MonacoEditor · LogsPanel · StrategyList · StrategyWorkspace · Sidebar
      pages/SystemSettings.tsx
      lib/cron.ts  project.ts  store.ts (zustand)  types/strategy.ts
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
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
make backend             # uvicorn :8000 (RELOAD=0 to disable --reload)

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
  platform's *only* way to run a strategy), keyed by the owning `--strategy-id`
  and `--schedule-id` (schedule ids are only unique per strategy). Verifies
  enabled/deployed/schedule state, acquires the OS lock, launches the
  strategy's own venv interpreter with a generated runtime config, and enforces
  the timeout. Cron redirects its output to the strategy's own
  `{strategies}/{strategy_id}/runner.log` — never a shared log sink.

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
- **Strategy workspace** (`src/components/StrategyWorkspace.tsx`, spec amendment
  v1.3) is a single full-page IDE workspace used for **both** creating and
  editing: one component, two routes. `+ New strategy` opens it empty via the
  `new-strategy` route; opening an existing strategy opens the **same** workspace
  populated via the `strategy` route (id = `route.id`). There is no creation
  modal, no separate edit page and no separate scheduling page.
- **Persistent header + a 4-tab bar** (only the active tab's body renders):
  **Code** — a resizable Monaco file explorer on the left and the Monaco editor
  (multiple open-file tabs, unsaved-change dots, create/rename/delete files and
  nested folders, ZIP upload) filling the rest; **Configuration** — name ·
  description · an *enabled* toggle (install crons on save) · parameters; the entry
  point, initial capital, runtime timeout, Python version and concurrent-runs policy
  are no longer user-editable here (the entry point is fixed to `main.py`; the
  others come from platform settings); **Schedules** — multiple independent
  Linux-cron schedules, each with a unique `SCH-NNN` id, name, cron expression,
  timezone (default `Asia/Kolkata`), an optional `command` (arguments passed to the
  entry point, split into an argument array — no shell), a live two-line preview
  of the resulting crontab entry (`CRON_TZ` + the cron line, plus the runner's
  second-stage launch command on the strategy's own venv) and an enabled toggle
  (add / edit / delete / enable-disable, ids preserved on edit); **Logs** — the
  single `strategy.log` with historical
  retrieval, live WebSocket stream, auto-scroll, pause/resume, search/filter and
  download.
- **One shared editing state** backs every tab; switching tabs never discards
  changes — the Code body stays mounted (the Monaco model survives; a `resize`
  event re-lays-out the editor when the Code tab is shown) and the Logs body is
  lazy-mounted. The unsaved indicator covers changes in *all* tabs, and **Save**
  persists the whole document (tree + configuration + schedules) into the
  strategy's single S3 JSON object: `strategiesApi.create` for a new strategy,
  otherwise an **ETag-conditional** `codeApi.save(id, files, etag)`
  (a mismatch → 409 "modified elsewhere") followed by metadata
  `strategiesApi.update` and a `schedulesApi.*` reconcile (drop removed, upsert by
  server id). **Deploy** saves first (blocking if the save fails) then
  `deployApi.deploy`, preserving the exact directory structure. **Cancel** returns
  to the strategy list and prompts "Discard changes / Keep editing" only when
  unsaved changes exist; it never saves or deploys. `StrategyDetail` and
  `components/tabs/` are removed.

### Data model (key identifiers & enums)
- **Strategy ID**: `STR-NNN` (3-digit, zero-padded, sequential).
- **Schedule ID**: `SCH-NNN`.
- **Deployment status**: `not_deployed | deployed | failed`.
- **Runtime states**: `not_deployed | stopped | running | failed | disabled`.
- **Entry point**: fixed to `main.py` (`DEFAULT_ENTRYPOINT`); not stored per
  strategy and not user-configurable. The strategy's own venv interpreter runs it.
- **Schedule `command`**: an optional string of arguments passed to the entry
  point when the schedule fires; the runner splits it into an argument array (no
  shell — golden rule 6) and appends it to the `main.py` invocation.
- **Runtime values** (Python version, run timeout, concurrency) come from platform
  **settings**, not the strategy document; the generated runtime config carries
  them for the running process.
- **Cron**: 5- or 6-field expression; default timezone `Asia/Kolkata`;
  default region `ap-south-1`. Each schedule installs `CRON_TZ=<tz>` on its own
  line before its entry (explicit timezone, independent of the host clock);
  production runs on cronie (Amazon Linux) which honours it — Debian/macOS
  crons ignore it for scheduling and fall back to host-local time.
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
  dependencies, **smoke-test import the entry point — a failure (or timeout)
  fails the deploy** (an unimportable strategy would crash on every cron tick),
  then sync into the live dir, reconcile cron, and mark `deployed` with the
  source hash. **Staging guarantees the live project is untouched on failure.**
- **Deploy progress is visible.** Every step (staging, venv creation, full pip
  stdout/stderr, smoke test, sync, reconcile, final status) is written to the
  strategy's own `strategy.log` tagged `DEPLOY` / `ERROR` — the single log the
  Logs tab tails (golden rule 4: never a second log sink).
- **Schedule reconcile** (`scheduler_service.reconcile`): regenerates the
  platform-owned crontab file from enabled schedules, pre-creating each
  strategy's directory so the cron redirect can't fail. Installation into the
  system crontab happens only when not in dry-run, and is a **managed-block
  merge**: only the lines between the `# BEGIN/END Quant Strategy Control
  Centre` markers are replaced, unrelated cron entries are preserved, and an
  empty schedule set removes the block entirely. Every schedule change
  (create/update/delete/enable/disable) and every deploy triggers a reconcile.
- **Start/stop are cron operations** (`deploy.py` → `SchedulerService`, spec
  §14): *start* enables the strategy's schedules and *stop* disables them —
  both reconcile the platform crontab. The platform never launches or kills a
  process itself.
- **Run trigger is cron-only.** The two-stage chain: `cron` → platform runner
  (`scripts/runner.py`, platform venv — it needs the platform's code for S3
  state checks, locking and timeouts) → the strategy's own venv interpreter
  running `main.py` with the schedule's args. The runner is the only thing that
  launches and (on timeout) kills a run: verify state → lock → launch own venv
  interpreter → write state JSON → enforce timeout. Its own stdout lands in the
  strategy's `runner.log` (cron redirect).
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
- [ ] If the cron-line template changed (`RUNNER_TEMPLATE`, `build_cron_entry`,
      `_install_system_cron`): update the frontend mirror `src/lib/cron.ts`
      (and its `GET /system/scheduler` path fields) in the same change.
- [ ] Keep spec-section citations and module docstrings when editing.
- [ ] Strategy create+edit use the single full-page `StrategyWorkspace`
      (4 tabs: Code / Configuration / Schedules / Logs); no modal, no
      separate edit or scheduling page, no fixed main.py structure.

---

## 10. Gotchas

- **`get_settings()` is cached** — it won't pick up env changes within a running
  process; restart uvicorn (or reload) after editing `.env`.
- **Scheduler dry-run** is the local default; a "successful" local reconcile only
  writes `runtime/platform.cron`, it does *not* install into your crontab.
- **CRON_TZ must be its own crontab line.** Prefixing it onto a schedule's
  entry line (`CRON_TZ=Asia/Kolkata * * * * …`) is invalid cron — the cron
  shell executes `CRON_TZ=Asia/Kolkata` as a command. `build_cron_entry`
  emits the timezone as a separate line before every entry; cronie applies it
  to the entries that follow.
- **Stop ≠ kill.** `stop`/`disable` only remove the strategy's cron lines; a run
  already in progress (fired by a prior tick) finishes on its own — it is never
  auto-killed and never auto-closes broker positions (spec §18/§22). To halt an
  in-flight run, use the kill switch or restart.
- **Runner paths follow `PLATFORM_ROOT`.** Cron lines are built from
  `scheduler_service.runner_paths()` (spec §4 layout under `PLATFORM_ROOT`,
  `venv` with a `.venv` fallback), not hard-coded `/opt/quant` — so a locally
  generated `runtime/platform.cron` line is actually runnable. The frontend
  preview fetches these paths from `GET /system/scheduler`.
- **Binary ZIP assets** are stored as `"\x00BINARY\x00" + base64(...)` — a
  deliberate marker; do not "clean it up" into plain base64.
- **`.npm-cache/`** exists so a root-owned `~/.npm` (a known npm bug) can't break
  the build; build via `make build`, not a bare `npm run build`.
- **`.env` must live in `platform/backend/`** (the CWD of `make backend`); `Settings` loads `.env` relative to CWD, so a repo-root `.env` is ignored. `make backend` now auto-provisions it via the `env` target, but if the backend is started by hand, run `make env` first. Without it the backend falls back to real AWS S3 with the placeholder `localstack`/`testpass` creds and fails with `InvalidAccessKeyId` (not a LocalStack problem).
- **The cron-invoked runner loads `.env` itself.** cron runs with a bare
  environment and `$HOME` as CWD, so `scripts/runner.py` explicitly loads
  `platform/backend/.env` (absolute path from its own location) before reading
  settings — otherwise it hits real AWS with placeholder creds and every tick
  dies with `InvalidAccessKeyId`.
- **A strategy's `main.py` is executed as a script** — defining `run(config)`
  without calling it does nothing on a tick. The starter template ships the
  contract: an `if __name__ == "__main__":` block invoking
  `run(load_config())`, where `load_config()` merges the Configuration-tab
  parameters (from the runtime config at `QUANT_RUNTIME_CONFIG`) with the
  schedule's CLI args (`--key value` → `config[key]`, valueless flags →
  `True`).
- Directory is spelled **"centre"** (British) on disk; the product name uses the
  same spelling. Keep it consistent.

- **Schedule-card crontab preview is a frontend mirror.**
  `platform/frontend/src/lib/cron.ts` re-implements
  `scheduler_service.RUNNER_TEMPLATE` / `build_cron_entry` (per-strategy
  `runner.log`; runner paths come from `GET /system/scheduler`, falling back to
  the spec §4 `/opt/quant` layout) so the preview shows the exact line the
  backend writes — keep the two in sync if the backend template changes.

- **Two different logs.** A cron run produces *runner.log* (the runner's own
  `STARTED/SKIP/ERROR` stdout, redirected by the cron line) and *strategy.log*
  (the strategy process's stdout/stderr, written by `process_service._launch`).
  A strategy's `print()` goes to `strategy.log`, never to `runner.log`.

- **Editor tree is decoded; the S3 store is base64.** Build the Monaco project
  tree from `codeApi.get(id)` (UTF-8 text), never from `doc.code.files` (raw
  base64); after a Save the local baseline is the decoded tree. Saves are
  ETag-conditional: `GET /strategies/{id}` returns the S3 ETag as a response
  header and `codeApi.save(id, files, etag)` is the
  conflict-detected write — never generate a new strategy id when editing.
