# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install
poetry install

# Run dev server
python main.py                          # Sanic on APP__PORT (default 1111)

# Run standalone scripts (require AgentBay context key)
poetry run python -m scripts.jd_link_search "关键词" --limit 5 --context-key "jd-context:default:xxx"
poetry run python -m scripts.taobao_link_search "关键词" --limit 5 --context-key "taobao-context:default:xxx"

# Lint
ruff check .
black --check .

# Docker
docker build -t micro-sniper .
```

## Architecture

**Stack**: Python 3.12 / Sanic (async web) / Tortoise-ORM (asyncpg) / Redis / Playwright + AgentBay SDK (cloud browser) / Alibaba OSS

**Entry**: `main.py` → `app.py:create_app()` wires routes, middleware, DB init, and Playwright startup.

**Two core abstractions**:

- **Context** (`models/context.py`): Persistent browser state (cookies, localStorage). Survives across sessions. Created during login, keyed by `platform-context:source:source_id`.
- **Session**: Ephemeral browser instance created from a Context via AgentBay. Deleted after each task with `sync_context=True` to flush cookies back.

**Task lifecycle** (`api/routes/sniper.py`):
1. `POST /api/tasks/` creates a Task record → dispatches via `asyncio.create_task`
2. `_run_task_with_context()` acquires context lock → runs script → releases in `finally`
3. Scripts use Playwright CDP for fast DOM extraction (`page.evaluate()`) and AgentBay `agent.act()` for complex interaction (clicking buttons, dismissing popups)
4. Results stored as `ProductLink` records; task result uploaded to OSS

**Hybrid browser automation pattern** (used in all scripts):
- Fast path: `page.evaluate(embedded_js)` for data extraction (~50ms)
- Smart path: `agent.act(ActOptions(action=instruction))` for UI interaction (~1-3s)
- After any `agent.act()` that may navigate the page, must re-fetch the Playwright page reference via `_get_*_page()` before calling `page.evaluate()` again — stale references cause `WriteUnixTransport closed` errors

**AI Native task model** (`models/task.py`):
- Task logs are natural language: purpose, action, result summary, next step
- `to_agent_readable()` produces LLM-friendly output
- Resume support: existing ProductLinks are deduplicated via `upsert_bulk()`, `start_page` param picks up from last page

**Key middleware**: `RequestContext` (request ID) → `Auth` (Bearer token from `SECURITY__API_KEY`) → `ExceptionHandler`

## Configuration

All via `.env` with `__` nested delimiter (pydantic-settings). Key prefixes: `APP__`, `DATABASE__`, `REDIS__`, `AGENTBAY__`, `SECURITY__`, `OSS__`, `TASK__`. See `config/settings.py` for all fields.

## Scripts (scripts/)

Each platform has a `_link_search.py` script (JD, Taobao, Tmall). They share the same structure:
- Accept keywords, limit, max_pages, context_key
- Navigate to search → sort by sales → paginate → extract links via embedded JS
- Pagination uses `agent.act("点击下一页")` for all platforms (not URL params — they don't work reliably)
- Resume from breakpoint: pass `start_page` to skip ahead via repeated "next page" clicks
- `_get_*_page()` helpers skip closed pages via `is_closed()` check to avoid stale CDP references
