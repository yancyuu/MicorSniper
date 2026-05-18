---
name: crawler-cli
description: Guides Micro-Sniper local crawler CLI usage. Use when running craw commands, using AgentBay act/extract/crawl, collecting comments or arbitrary page data from the command line, diagnosing browser contexts, or handling crawler CLI sessions.
---

# Crawler CLI

Use this skill for **local CLI usage only**. It teaches how to operate `craw`; code implementation rules belong in `AGENTS.md`.

## Install

Run from the repository root:

```bash
poetry install
poetry run craw --help
```

If Poetry warns that `craw` is defined but not installed as a script, run `poetry install` again.

Fallback module entry:

```bash
poetry run python -m scripts.crawler_cli --help
```

## Mental Model

- `craw agent act`: change page state.
- `craw agent extract`: read structured data from the current page.
- `craw agent crawl`: loop `extract -> next_action -> act -> extract`.
- `craw agent close`: close a kept AgentBay session.
- `craw context doctor`: diagnose DB, Redis login session, AgentBay context/session.

Do not use CLI mode as a production bulk scraper. It is for exploration, diagnosis, and small bounded crawls.

## Common Workflow

First check context health:

```bash
poetry run craw context list
poetry run craw context doctor <context-id>
poetry run craw context verify <context-id> --platform <platform>
```

Then choose one mode.

### Manual: act then extract

Use this when you want control over page state.

```bash
poetry run craw agent act \
  --context <context-id> \
  --url <start-url> \
  --action "搜索 SKG 按摩仪，并进入相关结果页" \
  --keep-session
```

Use the returned `session_id`:

```bash
poetry run craw agent extract \
  --session-id <session-id> \
  --goal "提取当前页面可见记录，包含标题、作者、正文、链接、时间和互动指标"
```

Close it:

```bash
poetry run craw agent close <session-id>
```

### Automatic: crawl

Use this when the next actions are obvious and bounded.

```bash
poetry run craw agent crawl \
  --context <context-id> \
  --start-url <url> \
  --goal "打开评论区，提取可见评论和回复。每条包含作者、正文、时间、点赞数；必要时滚动加载更多" \
  --max-steps 5 \
  --extract-timeout 90
```

Keep `--max-steps` conservative. Prefer 3-5 for exploration.
Use `--extract-timeout 90` or higher for heavy pages such as long comment threads.

## Extract Output

CLI extraction uses generic `records`, not product-only fields.

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

Rules:

- Put stable platform IDs in `record_id` when visible or derivable from the URL.
- `url` must be a real `http(s)` URL or an in-site path starting with `/`.
- Never treat `dom_index:*` or element indexes as URLs.
- If only an element reference is visible, put it in `fields.dom_ref`, then use `agent act` to open it before extracting again.
- For comments, use `record_type=comment`; put body in `text`, commenter in `author`, visible time in `published_at`, likes/reply count in `metrics`, nested replies in `children`.

## Context State

View or change context availability:

```bash
poetry run craw context status <context-id>
poetry run craw context status <context-id> --set logged_in
poetry run craw context status <context-id> --set disabled
poetry run craw context status <context-id> --set pending
```

If a kept session is stale:

```bash
poetry run craw agent close <session-id>
poetry run craw context release <context-id>
```

## Safety

Prefer:

- Clear, bounded goals.
- Low `--max-steps`.
- Manual `act` followed by `extract` for debugging.
- Stop on login, captcha, or verification prompts.

Avoid:

- "获取所有评论" without a step limit.
- Infinite scroll goals.
- Running CLI mode as high-volume production scraping.
