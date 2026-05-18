# Agent Development Guide

This file guides agents that modify crawler code in this repository. User-facing CLI usage belongs in `.claude/skills/crawler-cli/SKILL.md`; implementation guidance belongs here.

## Two Modes

Micro-Sniper has two crawler modes:

- **Service mode**: `poetry run python main.py`. This is the production path for scheduled and batch jobs. It may use Playwright/CDP, `page.evaluate()`, and platform-specific services for speed and stability.
- **Local CLI mode**: `craw ...` after installing the local shim with `scripts/install_craw_cli.sh`. This is the exploration path. It should prefer AgentBay `agent.act()` and `agent.extract()` and avoid hardcoded third-party DOM rules.

Do not mix the two without a reason. If the user asks for generic crawling, comments, unknown sites, or exploratory behavior, work in CLI mode.

## CLI Mode Principles

CLI mode is a local operator tool, not a production scraper engine.

- Use `agent.act()` to change browser state.
- Use `agent.extract()` to read structured data from the current page.
- Use `agent.navigate()` to open URLs.
- Keep `craw agent crawl` as a small orchestration loop: extract, inspect `done` / `next_action`, act, repeat.
- Do not add CSS selectors, XPath selectors, or `page.evaluate()` extraction to generic CLI commands.
- Keep site-specific logic out of CLI mode unless the command is explicitly platform-specific.

## Generic Extract Schema

Use generic records for CLI extraction. Do not make the schema product-only.

Preferred record shape:

```json
{
  "record_type": "comment | reply | product | note | user | link | text",
  "title": "",
  "text": "",
  "url": "",
  "author": "",
  "published_at": "",
  "metrics": {},
  "fields": {},
  "children": [],
  "source": "",
  "confidence": 1.0
}
```

For comments:

- `record_type`: `comment` or `reply`
- `text`: comment body
- `author`: commenter name
- `published_at`: visible time
- `metrics`: likes, reply count, badges, location, etc.
- `children`: visible nested replies

For links:

- `url` must be a real `http(s)` URL or an in-site path starting with `/`.
- Never store `dom_index:*` or element indexes in `url`.
- If AgentBay returns an element reference only, store it in `fields.dom_ref` and let `next_action` / `agent act` open it before extracting the real URL.
- Do not store CSS selectors, XPath, or locator hints like `h1 + p` as business fields. If needed for debugging, keep them as `fields.locator_hint`, not `fields.dom_ref`.

For stable IDs:

- Do not output a separate `record_id` in CLI extraction records.
- Prefer real `url`; platform-native IDs should be parsed from `url` downstream when needed.
- Do not invent IDs from titles. If only `dom_index:*` is available, open the detail page first.

## AgentBay Session Hygiene

For commands that keep sessions alive:

- Always print `session_id` and `browser_url`.
- Provide a matching close path, currently `craw agent close <session-id>`.
- Do not leave sessions alive by default.
- If a command fails after creating a session, delete it unless `--keep-session` was explicitly requested.

For context issues:

- Check DB `BrowserContext` first.
- Check Redis `login_session:*` next.
- Check AgentBay context/session last.
- Prefer adding diagnostics to `craw context doctor` or `craw doctor ...` rather than scattering one-off scripts.
- Use `craw context status <context-id> --set <status>` for manual context state changes instead of ad-hoc database updates.

## Safety Defaults

CLI mode can still trigger platform risk.

- Default `--max-steps` should remain small.
- Default Agent extract timeout should be 120 seconds. Expose `--extract-timeout` for heavier pages instead of letting extract exceptions crash the command.
- Keep `--extract-mode both` as the default. Use `text` to reduce visual overhead, or `vision` when text extraction returns DOM artifacts.
- Prefer explicit user goals over broad prompts.
- Stop or surface diagnosis on login, captcha, or verification prompts.
- Avoid unbounded goals like "get all comments" unless there is a step limit and the user accepts the risk.
- Do not add infinite scroll loops without clear limits.

## Agent Extract Error Handling

`agent.extract()` can time out on heavy pages. Do not let a single extract failure crash `craw agent crawl`.

Required behavior for crawl loops:

- Wrap each `agent.extract()` call in `try/except`.
- Log the failed round to `Task.logs`.
- Run a generic recovery action with `agent.act()`.
- Continue to the next round until `done=true` or `--max-steps` is reached.
- Let users raise the per-round timeout with `--extract-timeout` when 120 seconds is not enough.

Use both text and vision extraction when supported by the SDK:

```python
ExtractOptions(
    instruction=instruction,
    schema=AgentExtractResult,
    use_text_extract=True,
    use_vision=True,
    timeout=extract_timeout,
)
```

## Where To Put Code

- CLI entry: `crawler_cli/cmd/root.py`
- CLI command modules: `crawler_cli/cmd/`
- CLI shared internals: `crawler_cli/internal/`
- Production task dispatch: `services/task_runner.py`
- Business task orchestration: `services/sniper_tasks.py`
- Platform keyword services: `services/keyword_search/`
- Product detail services: `services/product_detail/`
- Context APIs: `api/routes/contexts.py`

When adding CLI features, prefer small subcommands over expanding one command with many modes.

## Testing

After changing CLI code, run:

```bash
python3 -m py_compile crawler_cli/cmd/root.py crawler_cli/cmd/*.py crawler_cli/internal/*.py
bash scripts/install_craw_cli.sh
craw --help
craw agent --help
python -m crawler_cli.cmd.root --help
```

For a safe smoke test:

```bash
craw agent extract \
  --context <context-id> \
  --url https://example.com \
  --goal "提取页面标题和主要说明，作为一条 text 记录" \
  --timeout 120
```

Do not use a high-risk platform page as the default smoke test.
