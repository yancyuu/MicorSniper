# -*- coding: utf-8 -*-
"""天猫关键词搜索链接采集 — thin wrapper。

业务逻辑在 services.keyword_search.tmall，本文件只保留 standalone CLI 入口。
"""

import asyncio
import argparse
import json
import os
from typing import Any

from config.settings import global_settings


async def run_tmall_link_search(task, ctx) -> dict[str, Any] | None:
    """供 task_runner 调用的入口。"""
    from services.keyword_search import get_keyword_search_service

    return await get_keyword_search_service("tmall").run(task, ctx)


# ===== Standalone CLI =====

_DEFAULT_KEYWORDS = ["SKG"]
_DEFAULT_CONTEXT_KEY = "tmall-context:default:placeholder"


async def _run_standalone() -> None:
    parser = argparse.ArgumentParser(description="Standalone Tmall product link search")
    parser.add_argument("keywords", nargs="*", default=_DEFAULT_KEYWORDS, help="搜索关键词")
    parser.add_argument("--context-key", default=os.getenv("TMALL_CONTEXT_KEY", _DEFAULT_CONTEXT_KEY))
    parser.add_argument("--limit", type=int, default=1000)
    parser.add_argument("--max-pages", type=int, default=100)
    parser.add_argument("--output", default="/tmp/tmall_links.json")
    args = parser.parse_args()

    keywords = args.keywords or _DEFAULT_KEYWORDS
    limit = min(args.limit, 1000)
    from services.keyword_search import get_keyword_search_service

    svc = get_keyword_search_service("tmall")

    from agentbay import AsyncAgentBay, BrowserContext as AgentBayContext, BrowserFingerprint, BrowserOption, BrowserScreen, CreateSessionParams
    from playwright.async_api import async_playwright

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    context_result = await agent_bay.context.get(args.context_key, create=False)
    if not context_result.success or not context_result.context:
        raise RuntimeError(f"Context not found: {args.context_key}")

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "standalone", "script": "tmall_link_search"},
            image_id=global_settings.agentbay.image_id,
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False),
        )
    )
    if not session_result.success:
        raise RuntimeError(f"Failed to create session: {session_result.error_message}")

    session = session_result.session
    playwright_instance = None
    browser = None

    try:
        print(f"Browser URL: {session.resource_url}")
        await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )
        endpoint_url = await session.browser.get_endpoint_url()
        playwright_instance = await async_playwright().start()
        browser = await playwright_instance.chromium.connect_over_cdp(endpoint_url)
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()
        browser_context.on("dialog", lambda dialog: dialog.dismiss())

        from services.keyword_search.base import _normalize_keywords

        kw_list = _normalize_keywords(keywords) or _DEFAULT_KEYWORDS
        agent = session.browser.agent
        results: list[dict[str, Any]] = []

        for keyword in kw_list:
            if len(results) >= limit:
                break
            remaining = limit - len(results)
            print(f"\nSearching keyword={keyword}, remaining={remaining}")
            search_url = svc.build_search_url(keyword)
            await agent.navigate(search_url)
            await asyncio.sleep(5)
            await svc.after_navigate(agent)

            page = await svc.get_page(browser_context, search_url)
            links = await svc.scroll_and_extract(agent, page, browser_context, search_url)
            for item in links:
                canonical = svc.canonicalize_url(item.get("url", ""))
                if canonical:
                    item["url"] = canonical
                    results.append(item)
            print(f"Found {len(links)} links, total={len(results)}")

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results[:limit], f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(results[:limit])} links to {args.output}")

    finally:
        async def _close_browser():
            if browser:
                cdp = await browser.new_browser_cdp_session()
                await cdp.send("Browser.close")
                await asyncio.sleep(0.5)
                await browser.close()

        try:
            await asyncio.wait_for(_close_browser(), timeout=10)
        except Exception:
            pass
        if playwright_instance:
            await playwright_instance.stop()
        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(_run_standalone())
