# -*- coding: utf-8 -*-
"""淘宝关键词搜索链接采集任务。

只负责通过关键词搜索淘宝商品链接，不进入详情页。页面动作使用 AgentBay agent，
页面数据用 JS 从 DOM 中提取，便于后续详情任务复用这些链接。
"""

import asyncio
import argparse
import json
import os
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from agentbay import (
    ActOptions,
    AsyncAgentBay,
    BrowserContext as AgentBayContext,
    BrowserFingerprint,
    BrowserOption,
    BrowserScreen,
    CreateSessionParams,
)
from config.settings import global_settings
from models.context import BrowserContext
from models.task import Task
from utils.logger import logger


_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 1000
_DEFAULT_MAX_PAGES = 100
_DEFAULT_KEYWORDS = ["SKG"]
_DEFAULT_CONTEXT_KEY = "taobao-context:default:3a4d9c23-c364-465e-90af-5bcbfbbcc73e"
_SCROLL_IDLE_ROUNDS = 2


_EXTRACT_PRODUCT_LINKS_JS = """
() => {
    const seen = new Set();
    const links = [];

    function normalizeHref(href) {
        try {
            const url = new URL(href, window.location.href);
            url.hash = '';
            return url.href;
        } catch (e) {
            return '';
        }
    }

    function isProductUrl(url) {
        try {
            const u = new URL(url);
            const host = u.hostname;
            const path = u.pathname;
            const isTaobao = host.endsWith('taobao.com') || host.endsWith('tmall.com') || host.endsWith('tmall.hk');
            if (!isTaobao) return false;
            return path.includes('item.htm') || path.includes('item_o.htm') || host.startsWith('detail.');
        } catch (e) {
            return false;
        }
    }

    function extractPrice(el) {
        const priceEl = el.querySelector('[class*="price"], [class*="Price"]');
        if (!priceEl) return '';
        const m = priceEl.textContent.match(/[\\d,.]+/);
        return m ? m[0] : '';
    }

    function extractSales(el) {
        const text = el.textContent || '';
        const m = text.match(/(\\d[\\d,]*\\+?)\\s*(人付款|人收货|人付款|月销|已售)/);
        return m ? m[1] + m[2] : '';
    }

    function extractShop(el) {
        const shopEl = el.querySelector('[class*="shop"], [class*="Shop"], [class*="store"], [class*="Store"]');
        return shopEl ? shopEl.textContent.trim().slice(0, 60) : '';
    }

    function extractTitle(el) {
        const titleEl = el.querySelector('[class*="title"], [class*="Title"]');
        return titleEl ? titleEl.textContent.trim().slice(0, 120) : '';
    }

    for (const a of document.querySelectorAll('a[href]')) {
        const url = normalizeHref(a.getAttribute('href'));
        if (!url || seen.has(url) || !isProductUrl(url)) continue;
        seen.add(url);

        const card = a.closest('[class*="Card"], [class*="card"], [class*="Content"], [class*="item"]');
        links.push({
            url,
            title: extractTitle(card || a) || (a.textContent || '').trim().slice(0, 80),
            price: extractPrice(card || a),
            sales: extractSales(card || a),
            shop: extractShop(card || a),
            card_text: card ? (card.textContent || '').trim().slice(0, 200) : '',
        });
    }

    const nextCandidates = Array.from(document.querySelectorAll('a, button, span, div')).filter((el) => {
        const text = (el.textContent || '').trim();
        const aria = el.getAttribute('aria-label') || '';
        return text === '下一页' || text.includes('下一页') || aria.includes('下一页');
    });
    const hasNext = nextCandidates.some((el) => {
        const cls = el.className ? String(el.className) : '';
        const disabled = el.getAttribute('disabled') !== null ||
            el.getAttribute('aria-disabled') === 'true' ||
            cls.includes('disabled') ||
            cls.includes('Disabled');
        return !disabled;
    });

    return {
        url: window.location.href,
        title: document.title,
        links,
        link_count: links.length,
        has_next: hasNext,
        skeleton_count: document.querySelectorAll('[class*="boneClass"], [class*="skeleton"], [class*="Skeleton"]').length,
        body_preview: (document.body ? document.body.innerText : '').slice(0, 300),
        scroll_y: window.scrollY,
        scroll_height: document.documentElement.scrollHeight || document.body.scrollHeight,
    };
}
"""


_CLICK_NEXT_PAGE_JS = """
() => {
    const candidates = Array.from(document.querySelectorAll('a, button, span, div')).filter((el) => {
        const text = (el.textContent || '').trim();
        const aria = el.getAttribute('aria-label') || '';
        return text === '下一页' || text.includes('下一页') || aria.includes('下一页');
    });

    for (const raw of candidates) {
        const el = raw.closest('a, button') || raw;
        const cls = el.className ? String(el.className) : '';
        const disabled = el.getAttribute('disabled') !== null ||
            el.getAttribute('aria-disabled') === 'true' ||
            cls.includes('disabled') ||
            cls.includes('Disabled');
        if (disabled) continue;
        if (typeof el.scrollIntoView === 'function') el.scrollIntoView({ block: 'center' });
        if (typeof el.click === 'function') {
            el.click();
        } else {
            el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
        }
        return true;
    }
    return false;
}
"""


async def run_taobao_link_search(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    """按关键词采集淘宝商品链接。"""
    params = task.params or {}
    keywords = _normalize_keywords(params.get("keywords")) or _DEFAULT_KEYWORDS
    limit = min(int(params.get("limit") or _DEFAULT_LIMIT), _MAX_LIMIT)
    max_pages = int(params.get("max_pages") or _DEFAULT_MAX_PAGES)

    if not keywords:
        await task.fail("No keywords provided")
        return None

    if not ctx.context_id:
        await task.fail("Context has no context_id, please login first")
        return None

    from sanic import Sanic

    app = Sanic.get_app()
    playwright = app.ctx.playwright
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    logger.info(f"[taobao_link_search] Using context_key={ctx.context_id}")
    context_result = await agent_bay.context.get(ctx.context_id, create=False)
    if not context_result.success or not context_result.context:
        await task.fail(f"Context not found: {ctx.context_id}")
        return None

    await task.log_step(1, "创建浏览器会话", {"context_id": ctx.context_id}, {}, "running")
    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "task", "task_type": task.task_type, "task_id": str(task.id)},
            image_id="browser_latest",
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False),
        )
    )
    if not session_result.success:
        await task.fail(f"Failed to create session: {session_result.error_message}")
        return None

    session = session_result.session
    browser = None

    try:
        task.browser_url = session.resource_url or ""
        await task.save()

        ok = await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(
                    devices=["desktop"],
                    operating_systems=["windows"],
                    locales=["zh-CN"],
                ),
            )
        )
        if not ok:
            await task.fail("Failed to initialize browser")
            return None

        endpoint_url = await session.browser.get_endpoint_url()
        browser = await playwright.chromium.connect_over_cdp(endpoint_url)
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()

        await task.log_step(1, "创建浏览器会话", {"context_id": ctx.context_id}, {"status": "ok"}, "completed")
        task.progress = 5
        await task.save()

        seen_urls: set[str] = set()
        results: list[dict[str, Any]] = []

        for index, keyword in enumerate(keywords):
            if len(results) >= limit:
                break

            step = index + 2
            remaining = limit - len(results)
            await task.log_step(step, f"采集淘宝商品链接: {keyword}", {"keyword": keyword, "remaining": remaining}, {}, "running")

            async def _on_page_collected(total_so_far: int):
                nonlocal results
                all_count = len(results) + total_so_far
                task.progress = min(95, int(5 + (index + total_so_far / max(remaining, 1)) / len(keywords) * 90))
                await task.save()

            keyword_links = await _collect_keyword_links(
                session=session,
                browser_context=browser_context,
                keyword=keyword,
                remaining=remaining,
                max_pages=max_pages,
                global_seen=seen_urls,
                on_page_done=_on_page_collected,
            )
            results.extend(keyword_links)

            await task.log_step(
                step,
                f"采集淘宝商品链接: {keyword}",
                {"keyword": keyword},
                {"found": len(keyword_links), "total": len(results)},
                "completed",
            )
            task.progress = min(95, int(5 + (index + 1) / len(keywords) * 90))
            task.result = {
                "items": results[:limit],
                "total": len(results),
                "limit": limit,
                "keywords": keywords,
            }
            await task.save()

        links = [item["url"] for item in results[:limit]]
        output = json.dumps(results[:limit], ensure_ascii=False, indent=2)
        return {
            "links": links,
            "items": results[:limit],
            "total": len(links),
            "limit": limit,
            "keywords": keywords,
            "output": output,
        }

    finally:
        try:
            if browser:
                cdp = await browser.new_browser_cdp_session()
                await cdp.send("Browser.close")
                await asyncio.sleep(0.5)
                await browser.close()
        except Exception as e:
            logger.warning(f"[taobao_link_search] Failed to close browser: {e}")

        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
        except Exception as e:
            logger.warning(f"[taobao_link_search] Failed to delete session: {e}")


async def _collect_keyword_links(
    session,
    browser_context,
    keyword: str,
    remaining: int,
    max_pages: int,
    global_seen: set[str],
    on_page_done=None,
) -> list[dict[str, Any]]:
    agent = session.browser.agent
    search_url = f"https://s.taobao.com/search?q={quote(keyword)}&sort=sale-desc"
    collected: list[dict[str, Any]] = []

    await agent.navigate(search_url)
    await asyncio.sleep(5)
    await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗")

    page = await _get_taobao_page(browser_context, search_url)
    page_index = 1
    empty_pages = 0

    while page_index <= max_pages and len(collected) < remaining:
        await _wait_search_page_ready(page)
        page_links = await _scroll_and_extract_page_links(page)

        new_count = 0
        for raw in page_links:
            canonical_url = _canonicalize_product_url(raw.get("url", ""))
            if not canonical_url or canonical_url in global_seen:
                continue
            global_seen.add(canonical_url)
            collected.append(
                {
                    "keyword": keyword,
                    "page": page_index,
                    "url": canonical_url,
                    "raw_url": raw.get("url", ""),
                    "title": raw.get("title", ""),
                    "price": raw.get("price", ""),
                    "sales": raw.get("sales", ""),
                    "shop": raw.get("shop", ""),
                }
            )
            new_count += 1
            if len(collected) >= remaining:
                break

        logger.info(
            f"[taobao_link_search] keyword={keyword} page={page_index} "
            f"new={new_count} page_links={len(page_links)} total={len(collected)}"
        )

        if new_count == 0:
            empty_pages += 1
        else:
            empty_pages = 0

        if on_page_done:
            await on_page_done(len(collected))

        if len(collected) >= remaining:
            break
        if empty_pages >= 3:
            logger.info(f"[taobao_link_search] keyword={keyword} 3 consecutive empty pages, stopping")
            break

        page_state = await page.evaluate(_EXTRACT_PRODUCT_LINKS_JS)
        if not page_state.get("has_next"):
            break

        moved = await _go_next_page(agent, page)
        if not moved:
            break

        page_index += 1
        await asyncio.sleep(4)
        page = await _get_taobao_page(browser_context, search_url)

    return collected


async def _scroll_and_extract_page_links(page) -> list[dict[str, Any]]:
    seen: set[str] = set()
    links: list[dict[str, Any]] = []
    idle_rounds = 0
    previous_height = 0

    for _ in range(30):
        state = await page.evaluate(_EXTRACT_PRODUCT_LINKS_JS)
        for item in state.get("links", []):
            url = item.get("url")
            if url and url not in seen:
                seen.add(url)
                links.append(item)

        scroll_height = int(state.get("scroll_height") or 0)
        if scroll_height and scroll_height == previous_height:
            idle_rounds += 1
        else:
            idle_rounds = 0
            previous_height = scroll_height

        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight || document.body.scrollHeight)")
        await asyncio.sleep(1.2)

        at_bottom = await page.evaluate(
            "() => window.innerHeight + window.scrollY >= (document.documentElement.scrollHeight || document.body.scrollHeight) - 4"
        )
        if at_bottom and idle_rounds >= _SCROLL_IDLE_ROUNDS:
            break

    state = await page.evaluate(_EXTRACT_PRODUCT_LINKS_JS)
    for item in state.get("links", []):
        url = item.get("url")
        if url and url not in seen:
            links.append(item)

    return links


async def _go_next_page(agent, page) -> bool:
    try:
        if await _agent_act(agent, "点击搜索结果列表底部的下一页按钮，进入下一页"):
            return True
    except Exception as e:
        logger.warning(f"[taobao_link_search] agent.act next page failed: {e}")

    try:
        return bool(await page.evaluate(_CLICK_NEXT_PAGE_JS))
    except Exception as e:
        logger.warning(f"[taobao_link_search] JS next page click failed: {e}")
        return False


async def _agent_act(agent, instruction: str) -> bool:
    ret = await agent.act(ActOptions(action=instruction))
    return bool(getattr(ret, "success", False))


async def _get_taobao_page(browser_context, fallback_url: str):
    for page in browser_context.pages:
        if "taobao.com" in page.url or "tmall.com" in page.url:
            return page

    page = await browser_context.new_page()
    await page.goto(fallback_url, timeout=60000, wait_until="domcontentloaded")
    await asyncio.sleep(3)
    return page


async def _wait_search_page_ready(page) -> None:
    try:
        await page.wait_for_function(
            """() => {
                const links = document.querySelectorAll('a[href*="item.htm"], a[href*="item_o.htm"], a[href*="detail.tmall"]');
                const body = document.body ? document.body.innerText : '';
                return links.length > 0 || body.includes('没有找到') || body.includes('登录');
            }""",
            timeout=15000,
        )
    except Exception:
        logger.warning("[taobao_link_search] Search page data wait timed out, extracting current DOM")


def _normalize_keywords(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _canonicalize_product_url(url: str) -> str:
    if not url:
        return ""

    parsed = urlparse(url)
    if not parsed.netloc:
        return ""

    query = parse_qs(parsed.query)
    item_id = (query.get("id") or query.get("item_id") or [""])[0]
    if not item_id:
        return ""

    host = parsed.netloc.lower()
    if "tmall" in host:
        canonical_host = "detail.tmall.com"
    else:
        canonical_host = "item.taobao.com"

    return urlunparse(("https", canonical_host, "/item.htm", "", urlencode({"id": item_id}), ""))


async def _run_standalone() -> None:
    parser = argparse.ArgumentParser(description="Standalone Taobao product link search")
    parser.add_argument("keywords", nargs="*", default=_DEFAULT_KEYWORDS, help="搜索关键词，默认 SKG")
    parser.add_argument("--context-key", default=os.getenv("TAOBAO_CONTEXT_KEY", _DEFAULT_CONTEXT_KEY))
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--max-pages", type=int, default=_DEFAULT_MAX_PAGES)
    parser.add_argument("--output", default="/tmp/taobao_links.json")
    args = parser.parse_args()

    from playwright.async_api import async_playwright

    keywords = _normalize_keywords(args.keywords) or _DEFAULT_KEYWORDS
    limit = min(args.limit, _MAX_LIMIT)
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    context_result = await agent_bay.context.get(args.context_key, create=False)
    if not context_result.success or not context_result.context:
        raise RuntimeError(f"Context not found: {args.context_key}")

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "standalone", "script": "taobao_link_search"},
            image_id="browser_latest",
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False),
        )
    )
    if not session_result.success:
        raise RuntimeError(f"Failed to create session: {session_result.error_message}")

    session = session_result.session
    playwright = None
    browser = None

    try:
        print(f"Browser URL: {session.resource_url}")
        await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(
                    devices=["desktop"],
                    operating_systems=["windows"],
                    locales=["zh-CN"],
                ),
            )
        )

        endpoint_url = await session.browser.get_endpoint_url()
        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(endpoint_url)
        browser_context = browser.contexts[0] if browser.contexts else await browser.new_context()

        seen_urls: set[str] = set()
        results: list[dict[str, Any]] = []
        for keyword in keywords:
            if len(results) >= limit:
                break
            remaining = limit - len(results)
            print(f"\nSearching keyword={keyword}, remaining={remaining}")
            keyword_links = await _collect_keyword_links(
                session=session,
                browser_context=browser_context,
                keyword=keyword,
                remaining=remaining,
                max_pages=args.max_pages,
                global_seen=seen_urls,
            )
            results.extend(keyword_links)
            print(f"Found {len(keyword_links)} new links, total={len(results)}")

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results[:limit], f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(results[:limit])} links to {args.output}")

    finally:
        try:
            if browser:
                cdp = await browser.new_browser_cdp_session()
                await cdp.send("Browser.close")
                await asyncio.sleep(0.5)
                await browser.close()
        except Exception:
            pass
        if playwright:
            await playwright.stop()
        await agent_bay.delete(session, sync_context=False)


if __name__ == "__main__":
    asyncio.run(_run_standalone())
