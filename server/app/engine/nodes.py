"""Node execution — each node maps to a crawl4ai method."""
from __future__ import annotations
import json
import base64
from typing import Any, Callable, Awaitable
from crawl4ai import (
    AsyncWebCrawler,
    CrawlerRunConfig,
    CacheMode,
    JsonCssExtractionStrategy,
    LLMExtractionStrategy,
    LLMConfig,
)
from app.config import settings


async def execute_crawl(
    crawler: AsyncWebCrawler,
    session_id: str,
    config: dict,
) -> dict:
    """crawl4ai.arun() — full page crawl, returns Markdown + screenshot."""
    url = config.get("url")
    if not url:
        raise ValueError("Crawl node missing url")

    wait_until = config.get("wait_until", "networkidle")
    run_config = CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        wait_until=wait_until,
        delay_before_return_html=2.0,
        screenshot=True,
    )
    result = await crawler.arun(url=url, config=run_config)

    markdown = ""
    if hasattr(result, "markdown") and result.markdown:
        markdown = result.markdown.raw_markdown if hasattr(result.markdown, "raw_markdown") else str(result.markdown)

    links = []
    if hasattr(result, "links") and result.links:
        links = result.links

    return {
        "success": True,
        "data": {
            "url": result.url if hasattr(result, "url") else url,
            "markdown_length": len(markdown),
            "markdown_preview": markdown[:500],
            "links_count": len(links) if isinstance(links, list) else 0,
            "links": links[:20] if isinstance(links, list) else [],
        },
        "screenshot": _extract_screenshot(result),
    }


async def execute_batch_crawl(
    crawler: AsyncWebCrawler,
    session_id: str,
    config: dict,
    upstream_urls: list[str] | None = None,
) -> dict:
    """crawl4ai.arun_many() — parallel crawl multiple URLs."""
    urls = []

    if config.get("use_upstream") and upstream_urls:
        urls = upstream_urls

    if not urls:
        raw = config.get("urls", "")
        urls = [u.strip() for u in raw.split("\n") if u.strip()]

    if not urls:
        raise ValueError("Batch Crawl node has no URLs")

    run_config = CrawlerRunConfig(
        cache_mode=CacheMode.BYPASS,
        screenshot=True,
    )

    results = await crawler.arun_many(urls=urls, config=run_config)

    items = []
    for r in results:
        if not r.success:
            continue
        md = ""
        if hasattr(r, "markdown") and r.markdown:
            md = r.markdown.raw_markdown if hasattr(r.markdown, "raw_markdown") else str(r.markdown)
        items.append({
            "url": r.url,
            "markdown_length": len(md),
            "markdown_preview": md[:300],
        })

    return {
        "success": True,
        "data": items,
        "screenshot": _extract_screenshot(results[-1]) if results else "",
    }


async def execute_css_extract(
    crawler: AsyncWebCrawler,
    session_id: str,
    config: dict,
) -> dict:
    """JsonCssExtractionStrategy — schema-based structured extraction.

    Self-healing: when CSS extraction returns empty, auto-fallback to AI extraction.
    """
    base_selector = config.get("base_selector")
    if not base_selector:
        raise ValueError("CSS Extract node missing base_selector")

    fields_raw = config.get("fields", "[]")
    try:
        fields = json.loads(fields_raw) if isinstance(fields_raw, str) else fields_raw
    except json.JSONDecodeError:
        raise ValueError("CSS Extract fields is not valid JSON")

    schema = {"name": "items", "baseSelector": base_selector, "fields": fields}
    strategy = JsonCssExtractionStrategy(schema, verbose=False)

    run_config = CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=strategy,
        js_only=True,
        screenshot=True,
    )
    result = await crawler.arun(url="", config=run_config)

    data = []
    if result.extracted_content:
        try:
            data = json.loads(result.extracted_content)
        except json.JSONDecodeError:
            data = [{"raw": result.extracted_content}]

    # Self-healing: if CSS extraction returned empty, fall back to AI extraction
    if not data:
        import logging
        logging.getLogger("nodes").warning("CSS extraction returned empty, falling back to AI extraction")
        ai_config = {
            "instruction": f"Extract structured data from the page. Base selector was: {base_selector}. Fields: {fields_raw}",
            "provider": settings.LLM_PROVIDER,
        }
        return await execute_ai_extract(crawler, session_id, ai_config)

    return {"success": True, "data": data, "screenshot": _extract_screenshot(result)}


async def execute_ai_extract(
    crawler: AsyncWebCrawler,
    session_id: str,
    config: dict,
) -> dict:
    """LLMExtractionStrategy — AI-powered structured extraction."""
    instruction = config.get("instruction", "")
    schema_raw = config.get("schema", "")
    provider = config.get("provider", settings.LLM_PROVIDER)

    schema = None
    if schema_raw:
        try:
            schema = json.loads(schema_raw) if isinstance(schema_raw, str) else schema_raw
        except json.JSONDecodeError:
            pass

    llm_config = LLMConfig(
        provider=provider,
        api_token=settings.OPENAI_API_KEY,
        base_url=settings.LLM_BASE_URL or None,
    )
    strategy = LLMExtractionStrategy(
        llm_config=llm_config,
        schema=schema,
        extraction_type="schema" if schema else "block",
        instruction=instruction,
        input_format="markdown",
    )

    run_config = CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        extraction_strategy=strategy,
        js_only=True,
        screenshot=True,
    )
    result = await crawler.arun(url="", config=run_config)

    data = None
    if result.extracted_content:
        try:
            data = json.loads(result.extracted_content)
        except json.JSONDecodeError:
            data = result.extracted_content

    return {"success": True, "data": data, "screenshot": _extract_screenshot(result)}


async def execute_js_execute(
    crawler: AsyncWebCrawler,
    session_id: str,
    config: dict,
) -> dict:
    """js_code — execute arbitrary JavaScript on current page."""
    js_code = config.get("js_code")
    if not js_code:
        raise ValueError("JS Execute node missing js_code")

    delay = config.get("delay", 1000) / 1000.0  # ms to seconds

    run_config = CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        js_code=js_code,
        js_only=True,
        delay_before_return_html=delay,
        screenshot=True,
    )
    result = await crawler.arun(url="", config=run_config)
    return {"success": True, "screenshot": _extract_screenshot(result)}


async def execute_paginate(
    crawler: AsyncWebCrawler,
    session_id: str,
    config: dict,
    child_extract_fn: Callable[[], Awaitable[dict]] | None = None,
) -> dict:
    """Multi-page loop — click next button + extract on each page."""
    next_js = config.get("next_js")
    if not next_js:
        raise ValueError("Paginate node missing next_js")

    max_pages = config.get("max_pages", 5)
    page_delay = config.get("page_delay", 2000) / 1000.0

    results = []

    for page_num in range(max_pages):
        # Extract from current page
        if child_extract_fn:
            page_result = await child_extract_fn()
            results.append(page_result.get("data", page_result))

        # Click next (skip on last page)
        if page_num < max_pages - 1:
            run_config = CrawlerRunConfig(
                session_id=session_id,
                cache_mode=CacheMode.BYPASS,
                js_code=next_js,
                js_only=True,
                delay_before_return_html=page_delay,
                screenshot=True,
            )
            try:
                await crawler.arun(url="", config=run_config)
            except Exception:
                break

    # Final screenshot
    run_config = CrawlerRunConfig(
        session_id=session_id,
        cache_mode=CacheMode.BYPASS,
        js_only=True,
        screenshot=True,
    )
    result = await crawler.arun(url="", config=run_config)

    return {"success": True, "data": results, "screenshot": _extract_screenshot(result)}


def _extract_screenshot(result) -> str:
    """Extract base64 screenshot from crawl4ai result."""
    if hasattr(result, "screenshot") and result.screenshot:
        if isinstance(result.screenshot, bytes):
            return base64.b64encode(result.screenshot).decode()
        return result.screenshot
    return ""
