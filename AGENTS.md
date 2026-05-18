# Agent Development Guide

This file guides agents that modify crawler code in this repository. User-facing CLI usage belongs in `.claude/skills/crawler-cli/SKILL.md`; implementation guidance belongs here.

## Two Modes

Micro-Sniper has two crawler modes:

- **Service mode**: `poetry run python main.py`. This is the production path for scheduled and batch jobs. It may use Playwright/CDP, `page.evaluate()`, and platform-specific services for speed and stability.
- **Local CLI mode**: `poetry run craw ...`. This is the exploration path. It should prefer AgentBay `agent.act()` and `agent.extract()` and avoid hardcoded third-party DOM rules.

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
  "record_id": "",
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

For stable IDs:

- Store platform-native IDs in `record_id` when visible or derivable from the URL.
- Examples: Xiaohongshu note ID, product ID, comment ID, reply ID, user ID.
- Do not invent IDs from titles. If only `dom_index:*` is available, leave `record_id` empty and open the detail page first.

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
- Expose and use `--extract-timeout` for heavy pages instead of letting extract exceptions crash the command.
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
- Let users raise the per-round timeout with `--extract-timeout`.

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

- CLI entry: `scripts/crawler_cli.py`
- Production task dispatch: `services/task_runner.py`
- Business task orchestration: `services/sniper_tasks.py`
- Platform keyword services: `services/keyword_search/`
- Product detail services: `services/product_detail/`
- Context APIs: `api/routes/contexts.py`

When adding CLI features, prefer small subcommands over expanding one command with many modes.

## Testing

After changing CLI code, run:

```bash
python3 -m py_compile scripts/crawler_cli.py
poetry run craw --help
poetry run craw agent --help
```

For a safe smoke test:

```bash
poetry run craw agent extract \
  --context <context-id> \
  --url https://example.com \
  --goal "提取页面标题和主要说明，作为一条 text 记录" \
  --timeout 30
```

Do not use a high-risk platform page as the default smoke test.
