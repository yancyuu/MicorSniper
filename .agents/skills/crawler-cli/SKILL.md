---
name: crawler-cli
description: Guides Micro-Sniper local crawler CLI usage. Use when running craw commands, using AgentBay act/extract/crawl, collecting comments or arbitrary page data from the command line, diagnosing browser contexts, or handling crawler CLI sessions.
---

# Crawler CLI

Use this skill for **local CLI usage only**. It teaches how to operate `craw`; code implementation rules belong in `AGENTS.md`.

## Install Or Update

Before using `craw`, the agent must ensure it is installed.

For a fresh machine or a new checkout:

```bash
git clone https://github.skg.com/skg-ai/rpa/MicroSniper.git
cd Micro-Sniper
bash scripts/install_craw_cli.sh
```

For an existing checkout, run from the repository root:

```bash
bash scripts/install_craw_cli.sh
```

Before using `craw`, check:

```bash
command -v craw
```

If the command is missing in an existing checkout, install it automatically:

```bash
bash scripts/install_craw_cli.sh
```

The installer:

1. Runs `poetry install`.
2. Writes a shim to `~/.local/bin/craw`.
3. Keeps dependencies inside Poetry instead of system Python.

Verify:

```bash
craw --help
```

If `craw` is not found, add `~/.local/bin` to `PATH`:

```bash
export PATH="$HOME/.local/bin:$PATH"
craw --help
```

To refresh after code changes, rerun:

```bash
bash scripts/install_craw_cli.sh
```

Agent workflow:

1. Run `command -v craw`.
2. If missing, run `bash scripts/install_craw_cli.sh`.
3. If `craw` is still missing, run `export PATH="$HOME/.local/bin:$PATH"` in the current shell.
4. Run `craw --help` before using other `craw` commands.

Module entry:

```bash
python -m crawler_cli.cmd.root --help
```

The implementation is split under `crawler_cli/cmd/` and `crawler_cli/internal/`.

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
craw context list
craw context doctor <context-id>
craw context verify <context-id> --platform <platform>
```

Then choose one mode.

### Manual: act then extract

Use this when you want control over page state.

```bash
craw agent act \
  --context <context-id> \
  --url <start-url> \
  --action "搜索 SKG 按摩仪，并进入相关结果页" \
  --keep-session
```

Use the returned `session_id`:

```bash
craw agent extract \
  --session-id <session-id> \
  --goal "提取当前页面可见记录，包含标题、作者、正文、链接、时间和互动指标" \
  --extract-mode both
```

Close it:

```bash
craw agent close <session-id>
```

### Automatic: crawl

Use this when the next actions are obvious and bounded.

```bash
craw agent crawl \
  --context <context-id> \
  --start-url <url> \
  --goal "打开评论区，提取可见评论和回复。每条包含作者、正文、时间、点赞数；必要时滚动加载更多" \
  --max-steps 5 \
  --extract-mode both \
  --extract-timeout 120
```

Keep `--max-steps` conservative. Prefer 3-5 for exploration.
Use `--extract-timeout 120` or higher for heavy pages such as long comment threads.
Use `--extract-mode both` by default. Try `text` if visual extraction is slow; try `vision` if text extraction returns DOM artifacts.

## Extract Mode Notes

Observed behavior from safe smoke tests:

- `--extract-mode both`: best default. It can combine visible text and page structure, and is more likely to return real links.
- `--extract-mode text`: faster, but may miss `href` links and return only a DOM hint in `fields.dom_ref`.
- `--extract-mode vision`: useful when text extraction is noisy, but may identify visible elements without resolving real URLs.

If URL is missing:

1. Treat the current result as a feed/list preview.
2. Use `craw agent act` to open the specific card or record.
3. Run `craw agent extract` again on the detail page.

## Extract Output

CLI extraction uses generic `records`, not product-only fields.

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

Rules:

- `url` must be a real `http(s)` URL or an in-site path starting with `/`.
- Never treat `dom_index:*` or element indexes as URLs.
- If only an element reference is visible, put it in `fields.dom_ref`, then use `agent act` to open it before extracting again.
- Do not treat CSS selectors, XPath, or hints like `h1 + p` as data. They may be kept as `fields.locator_hint` only for debugging, not as extracted business data.
- For comments, use `record_type=comment`; put body in `text`, commenter in `author`, visible time in `published_at`, likes/reply count in `metrics`, nested replies in `children`.

## Context State

View or change context availability:

```bash
craw context status <context-id>
craw context status <context-id> --set logged_in
craw context status <context-id> --set disabled
craw context status <context-id> --set pending
```

If a kept session is stale:

```bash
craw agent close <session-id>
craw context release <context-id>
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
