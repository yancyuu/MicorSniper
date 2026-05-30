"""AI node-graph generation (FR-7, FR-8).

Given a URL + natural-language goal, fetch the page (reusing a logged-in
session when given), detect a login wall, ask crawl4ai's LLM to synthesize a
CSS extraction schema, dry-run it on the fetched HTML for a sample, and
assemble a `context → crawl → css_extract (+ paginate)` node graph.
"""
from __future__ import annotations
import asyncio
import json
import re
from typing import Any
from crawl4ai import (
    AsyncWebCrawler,
    BrowserConfig,
    CrawlerRunConfig,
    CacheMode,
    JsonCssExtractionStrategy,
    LLMConfig,
)
from app.store import STORE
from app.config import settings


_LOGIN_PATH_HINTS = ("login", "signin", "sign-in", "passport", "/auth", "sso", "account/login")
_LOGIN_HOST_HINTS = ("passport.", "login.", "auth.", "account.")
_LOGIN_TITLE_HINTS = ("登录", "請登入", "请登录", "sign in", "log in", "login")
_PAGINATION_HINTS = (
    'rel="next"', "rel='next'", "下一页", "下一頁", "next page",
    'class="next"', "pagination", "pager", 'aria-label="next"',
)


async def _browser_config_for(session_name: str | None) -> BrowserConfig:
    """Resolve a BrowserConfig consistent with runner session binding (FR-3)."""
    common = dict(
        headless=True,
        viewport_width=1280,
        viewport_height=800,
        extra_args=["--disable-blink-features=AutomationControlled"],
    )
    if session_name:
        profile_dir = STORE.profiles_dir / session_name
        if profile_dir.is_dir():
            return BrowserConfig(
                use_persistent_context=True, user_data_dir=str(profile_dir), **common
            )
        session = await STORE.load_session(session_name)
        if session and session.cookies:
            # Sanitize cookies: Playwright requires expires to be float/int, not None
            clean_cookies = []
            for c in session.cookies:
                c = dict(c)  # copy
                if c.get("expires") is None:
                    c["expires"] = -1
                clean_cookies.append(c)
            return BrowserConfig(cookies=clean_cookies, **common)
    return BrowserConfig(**common)


def detect_login_wall(html: str, final_url: str) -> bool:
    """Heuristic login-wall detection (FR-8)."""
    u = (final_url or "").lower()
    if any(p in u for p in _LOGIN_PATH_HINTS):
        return True
    if any(h in u for h in _LOGIN_HOST_HINTS):
        return True
    low = (html or "").lower()
    if 'type="password"' in low or "type='password'" in low:
        return True
    m = re.search(r"<title[^>]*>(.*?)</title>", low, re.S)
    if m and any(k in m.group(1) for k in _LOGIN_TITLE_HINTS):
        return True
    return False


def detect_pagination(html: str) -> tuple[bool, str | None, int | None]:
    """Detect a pagination control and emit a click-next script (FR-7, fix C2)."""
    low = (html or "").lower()
    if not any(h in low for h in _PAGINATION_HINTS):
        return False, None, None

    next_js = (
        "(() => {"
        "const sels = ['a[rel=next]','a[rel=\"next\"]','.next','.pagination-next',"
        "'.pager-next','[aria-label*=next i]','[aria-label*=\"下一页\"]'];"
        "for (const s of sels){const el=document.querySelector(s);"
        "if(el){el.click(); return true;}}"
        "const cand=[...document.querySelectorAll('a,button')];"
        "const t=cand.find(a=>/下一页|下一頁|next|»/i.test((a.textContent||'').trim()));"
        "if(t){t.click(); return true;} return false;"
        "})()"
    )
    return True, next_js, 3


def _generate_schema_sync(html: str, goal: str) -> dict:
    llm_config = LLMConfig(
        provider=settings.LLM_PROVIDER,
        api_token=settings.OPENAI_API_KEY,
        base_url=settings.LLM_BASE_URL or None,
    )
    schema = JsonCssExtractionStrategy.generate_schema(
        html=html, query=goal, llm_config=llm_config
    )
    return schema or {}


async def _run_sample(crawler: AsyncWebCrawler, html: str, schema: dict) -> list[Any]:
    """Dry-run the generated schema against the fetched HTML (FR-7)."""
    if not schema or not schema.get("fields"):
        return []
    strategy = JsonCssExtractionStrategy(schema, verbose=False)
    run_cfg = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, extraction_strategy=strategy)
    res = await crawler.arun(url="raw://" + html, config=run_cfg)
    if not res.extracted_content:
        return []
    try:
        data = json.loads(res.extracted_content)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def build_graph(
    url: str,
    schema: dict,
    session_name: str | None = None,
    paginated: bool = False,
    next_js: str | None = None,
    max_pages: int | None = None,
) -> dict:
    """Assemble nodes/edges: context? → crawl → (paginate →)? css_extract."""
    nodes: list[dict] = []
    edges: list[dict] = []

    def add(ntype: str, label: str, config: dict) -> str:
        nid = f"node_{len(nodes) + 1}"
        nodes.append({
            "id": nid,
            "type": "workflowNode",
            "position": {"x": len(nodes) * 260, "y": 0},
            "data": {"type": ntype, "label": label, "status": "idle", "config": config},
        })
        return nid

    def connect(a: str, b: str) -> None:
        edges.append({"id": f"e{len(edges) + 1}", "source": a, "target": b})

    css_config = {
        "base_selector": schema.get("baseSelector", ""),
        "fields": json.dumps(schema.get("fields", []), ensure_ascii=False, indent=2),
    }

    chain: list[str] = []
    if session_name:
        chain.append(add("context", "上下文", {"session_name": session_name}))
    chain.append(add("crawl", "爬取页面", {"url": url}))

    if paginated and next_js:
        chain.append(add("paginate", "翻页采集", {"next_js": next_js, "max_pages": max_pages or 3}))
        css_id = add("css_extract", "CSS 提取", css_config)
        for a, b in zip(chain, chain[1:]):
            connect(a, b)
        connect(chain[-1], css_id)  # css_extract is the paginate node's child
    else:
        chain.append(add("css_extract", "CSS 提取", css_config))
        for a, b in zip(chain, chain[1:]):
            connect(a, b)

    return {"nodes": nodes, "edges": edges}


def _is_low_confidence(schema: dict, sample: list) -> bool:
    if not schema or not schema.get("fields"):
        return True
    return not sample


async def generate_workflow(url: str, goal: str, session_name: str | None = None) -> dict:
    """Orchestrate fetch → login-detect → schema → sample → graph (FR-7, FR-8)."""
    browser_config = await _browser_config_for(session_name)

    async with AsyncWebCrawler(config=browser_config) as crawler:
        fetch_cfg = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            wait_until="domcontentloaded",
            screenshot=True,
        )
        res = await crawler.arun(url=url, config=fetch_cfg)
        html = res.html or ""
        final_url = res.url or url

        if detect_login_wall(html, final_url) and not session_name:
            return {
                "need_session": True,
                "message": "目标页面需要登录,请先选择一个已登录会话再生成。",
            }

        schema = await asyncio.to_thread(_generate_schema_sync, html, goal)
        paginated, next_js, max_pages = detect_pagination(html)
        sample = await _run_sample(crawler, html, schema)
        low_confidence = _is_low_confidence(schema, sample)
        graph = build_graph(url, schema, session_name, paginated, next_js, max_pages)

    return {
        **graph,
        "schema": schema,
        "sample": sample,
        "low_confidence": low_confidence,
    }
