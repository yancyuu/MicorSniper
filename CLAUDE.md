# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install (Python backend)
cd server && poetry install && poetry run playwright install chromium

# Dev (frontend + backend together, from repo root)
npm run dev                 # concurrently: FastAPI :1111 + Vite :3000
npm run dev:server          # backend only — cd server && poetry run python main.py
npm run dev:frontend        # frontend only — Vite on :3000

# Build frontend (served by FastAPI from frontend/dist in production)
npm run build

# Start (production single process — FastAPI serves API + built frontend)
npm run start
```

## Architecture

Canvas RPA: a React + React Flow canvas drives a single **crawl4ai** backend. The
user wires a node graph (or has the AI generate one), runs it, and the backend
executes nodes in topological order with one cloud/headless browser.

**Stack**: Python 3.12 / FastAPI + uvicorn / crawl4ai 0.8.6 (Playwright Chromium) /
JSON-file persistence under `data/` · Frontend: React + TypeScript + Vite +
@xyflow/react (React Flow) + Tailwind.

**Layout**:
- `server/` — FastAPI app (`server/main.py:create_app()` uses a `lifespan` that calls `STORE.init()`).
  - `server/app/routes/` — `workflows.py` (CRUD + `/run` + `/generate` + `/{id}/stop`), `sessions.py` (login/context browsers), `results.py` (run results + export).
  - `server/app/engine/` — `runner.py` (topo-sort + sequential node execution), `nodes.py` (one executor per node type), `refs.py` (`{{node.path}}` data references), `generate.py` (AI node-graph synthesis).
  - `server/app/ws/handlers.py` — `ConnectionManager` broadcasting per-node status frames over `WS /ws/workflows/{id}`.
  - `server/app/store.py` — file-backed store for workflows, sessions, browser profiles, and run results.
- `frontend/src/` — `hooks/useWorkflow.ts` (canvas + run/stop/generate state), `components/` (Canvas, BottomBar, ConfigPanel, GeneratePanel, BrowserFrame, …).

**Node types** (`models.py` `NodeType`, mirrored in `frontend/src/types/workflow.ts`):
`context` · `crawl` · `batch_crawl` · `css_extract` · `ai_extract` · `js_execute` · `paginate`.

**Context node & sessions (FR-3)**: A `context` node binds a logged-in session.
Sessions are created via `POST /api/sessions/login` (headed browser for manual
login) → `confirm-login` saves cookies + persists the browser profile under
`data/browser_profiles/{name}`. At run time `runner._build_browser_config`:
1. context node with an existing profile dir → persistent context;
2. profile missing but stored cookies present → `BrowserConfig(cookies=...)` injection;
3. neither → broadcast a session-invalid error;
4. no context node → ephemeral `run-{workflow_id}` profile.

**Run lifecycle**:
1. `POST /api/workflows/run` → registers an asyncio task in `_RUNNING`, returns `workflow_id`; client connects `WS /ws/workflows/{id}`.
2. `runner.run_workflow` topo-sorts nodes, skips `context` nodes and paginate-owned extract children (the latter run inside `execute_paginate` — avoids double execution, FR-4), resolves `{{node.path}}` refs in each node's config (FR-5), executes, and broadcasts `running/success/error` frames per node plus a terminal `root` frame.
3. On completion/failure/stop it builds a `RunResult` and `STORE.save_result()` (FR-6); `store.rotate_results()` evicts oldest by `RESULTS_MAX_RUNS` / `RESULTS_MAX_MB`.
4. `POST /api/workflows/{id}/stop` cancels the task; the runner catches `CancelledError`, broadcasts `Workflow stopped`, and persists the partial run (FR-9).

**Data references (FR-5)** — `engine/refs.py`: any node config string may contain
`{{node_id.dotted.path}}`. Root is the referenced node's full result dict, so
`{{n3.data}}` is the data and `{{n3.data[].href}}` projects the `href` column
out of a list. Missing refs resolve to empty + a warning frame.

**AI generation (FR-7/8)** — `POST /api/workflows/generate` (`engine/generate.py`):
fetch the page (reusing the session) → `detect_login_wall` (returns
`{need_session:true}` when a login wall is hit with no session) → crawl4ai
`JsonCssExtractionStrategy.generate_schema(query=goal)` → `detect_pagination`
(emits a `paginate` node with `next_js` + `max_pages` only when a pager is
found) → dry-run the schema on the fetched HTML for a `sample` → `build_graph`
assembles `context → crawl → (paginate →)? css_extract`. Empty sample / missing
fields ⇒ `low_confidence:true` (schema fields are page-grounded, never fabricated).

**Results (FR-6)** — `GET /api/results/{id}`, `GET /api/results/{id}/export?format=json|csv`, `GET /api/results/`.

## Configuration

`server/.env` via pydantic-settings (`server/app/config.py`), no nested prefix:
`PORT` (1111) · `DEBUG` · `DATA_DIR` (`../data`) · `LLM_PROVIDER` (e.g.
`openai/gpt-4o-mini`) · `OPENAI_API_KEY` (required for `ai_extract` and
`/generate`) · `RESULTS_MAX_RUNS` (200) · `RESULTS_MAX_MB` (500).

## Conventions

- **Node executors** (`engine/nodes.py`): one `async def execute_<type>(crawler, session_id, config)` per node, returning `{success, data, screenshot}`. `css_extract`/`ai_extract`/`js_execute`/`paginate` run against the live session via `js_only=True` (no re-navigation).
- **crawl4ai screenshots** come back base64; `_extract_screenshot` normalizes bytes→base64.
- **Raw-HTML dry runs** use the `raw://<html>` URL scheme to extract without a second navigation.
- **Spec**: feature work tracked under `specs/1-ai-crawl-nodes/` (spec.md, plan.md, tasks.md, contracts/api.md).
