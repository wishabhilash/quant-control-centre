# Quant Strategy Control Centre — developer convenience targets (spec section 17).
# Local S3 emulation uses LocalStack (S3 API on :4566).

SHELL := /bin/bash
PYTHON ?= python3
BACKEND_DIR := platform/backend
FRONTEND_DIR := platform/frontend
LOCALSTACK_PORT ?= 4566
LOCALSTACK_USER ?= localstack
LOCALSTACK_PASS ?= testpass
S3_BUCKET ?= quant-platform-dev
API_PORT ?= 8000
VITE_PORT ?= 5173
RELOAD ?= 1   # auto-reload for `make backend` (override: make backend RELOAD=0)
RELOAD_FLAG := $(if $(findstring 1,$(RELOAD)),--reload,)
NPM_CACHE ?= $(CURDIR)/.npm-cache
# Project-local, writable npm cache so a root-owned system cache (~/.npm, a
# known npm bug) can't break the build. Override: make build NPM_CACHE=/path

# Backend interpreter: prefer the project venv at $(BACKEND_DIR)/.venv (it ships
# uvicorn + boto3); fall back to the system $(PYTHON). Absolute path (via $(CURDIR))
# so it works regardless of a recipe's CWD. Shared by `backend` and the LocalStack helper.
BUCKET_SCRIPT := scripts/ensure_localstack_bucket.py
VENV_PYTHON := $(CURDIR)/$(BACKEND_DIR)/.venv/bin/python
ifeq ($(wildcard $(VENV_PYTHON)),)
BACKEND_PYTHON := $(PYTHON)
else
BACKEND_PYTHON := $(VENV_PYTHON)
endif
BUCKET_PYTHON := $(BACKEND_PYTHON)

.PHONY: help localstack localstack-up localstack-bucket localstack-down env backend frontend build lock test lint clean

help:
	@echo 'Targets:'
	@echo '  localstack      Start LocalStack and create the development bucket'
	@echo '  env             Ensure platform/backend/.env exists (from .env.development; never clobbers an existing .env)'
	@echo '  localstack-down Stop the LocalStack container (data volume preserved)'
	@echo '  backend         Run the FastAPI backend (uvicorn, auto-reload)'
	@echo '  frontend        Run the Vite dev server (Monaco IDE)'
	@echo '  build           Build the frontend into platform/frontend/dist (isolated npm cache)'
	@echo '  lock            Generate/refresh the frontend package-lock.json'
	@echo '  test            Run backend pytest suite'
	@echo '  lint            Typecheck the frontend (tsc --noEmit)'
	@echo '  clean           Remove build + cache artefacts'
	@echo ''
	@echo 'LocalStack S3: http://localhost:$(LOCALSTACK_PORT)  (creds $(LOCALSTACK_USER)/$(LOCALSTACK_PASS))'


## Ensure a local dev .env exists in the backend dir (idempotent; the backend
## reads .env relative to its CWD, which is platform/backend under `make backend`).
env:
	@[ -f "$(BACKEND_DIR)/.env" ] || cp .env.development "$(BACKEND_DIR)/.env"
	@echo "Ensured $(BACKEND_DIR)/.env (created from .env.development if it was missing)."

## Start LocalStack (S3-compatible), wait for readiness, and create the dev bucket.
localstack: localstack-up localstack-bucket

localstack-up:
	docker run -d --name quant-localstack \
	  -p $(LOCALSTACK_PORT):4566 \
	  -v quant-localstack-data:/data \
	  -e LOCALSTACK_AUTH_TOKEN=${LOCALSTACK_AUTH_TOKEN} \
	  localstack/localstack:latest
	echo 'Waiting for LocalStack to become ready...'
	@for i in $$(seq 1 30); do curl -fsS http://localhost:$(LOCALSTACK_PORT)/_localstack/health >/dev/null 2>&1 && break; sleep 1; done

## Create the development bucket with versioning + blocked public access (idempotent).
localstack-bucket:
	@echo "Ensuring LocalStack bucket $(S3_BUCKET) on http://localhost:$(LOCALSTACK_PORT) "\
	        "(interpreter: $(BUCKET_PYTHON))..."
	@"$(BUCKET_PYTHON)" "$(BUCKET_SCRIPT)" \
	    --port "$(LOCALSTACK_PORT)" \
	    --bucket "$(S3_BUCKET)" \
	    --user "$(LOCALSTACK_USER)" \
	    --pass "$(LOCALSTACK_PASS)"

## Stop the LocalStack container (data volume is preserved).
localstack-down:
	docker rm -f quant-localstack || true

## Run the backend (uvicorn, auto-reload by default; disable with: make backend RELOAD=0).
## Uses the venv interpreter at $(BACKEND_DIR)/.venv (falls back to system $(PYTHON)).
backend: env
	cd $(BACKEND_DIR) \
	  && "$(BACKEND_PYTHON)" -m uvicorn app.main:app --host 0.0.0.0 --port $(API_PORT) $(RELOAD_FLAG)

## Run the Vite dev server (proxies /api -> :$(API_PORT)).
frontend:
	cd $(FRONTEND_DIR) \
	  && API_PROXY_TARGET=http://localhost:$(API_PORT) npx vite --port $(VITE_PORT)

## Build the frontend into platform/frontend/dist. Uses `npm ci` when a
## package-lock.json is present (reproducible) and falls back to `npm install`.
build:
	cd $(FRONTEND_DIR) \
	  && if [ -f package-lock.json ]; then npm ci --cache "$(NPM_CACHE)"; else npm install --cache "$(NPM_CACHE)"; fi \
	  && npm run build

## Generate/refresh the frontend package-lock.json (commit it for reproducible npm ci).
lock:
	cd $(FRONTEND_DIR) && npm install --package-lock-only --cache "$(NPM_CACHE)"

test:
	cd $(BACKEND_DIR) \
	  && "$(BACKEND_PYTHON)" -m pytest

lint:
	cd $(FRONTEND_DIR) && npm run typecheck

clean:
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(FRONTEND_DIR)/dist $(FRONTEND_DIR)/node_modules/.vite

