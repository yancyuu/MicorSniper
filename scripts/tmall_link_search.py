# -*- coding: utf-8 -*-
"""天猫关键词搜索链接采集任务。

只负责通过关键词搜索天猫商品链接，不进入详情页。页面动作使用 AgentBay agent，
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
from models.task import Task, TaskStatus
from models.product_link import ProductLink, ProductLinkMonitorStatus, ProductLinkSourceType
from utils.logger import logger
from utils.login_check import check_login_status, login_failed_message


_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 1000
_DEFAULT_MAX_PAGES = 100
_DEFAULT_KEYWORDS = ["SKG"]
_DEFAULT_CONTEXT_KEY = "tmall-context:default:placeholder"
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
            return (host.endsWith('tmall.com') || host.endsWith('tmall.hk'))
                && (path.includes('item.htm') || path.includes('item_o.htm') || host.startsWith('detail.'));
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
        const m = text.match(/(\\d[\\d,]*\\+?)\\s*(人付款|人收货|月销|已售|笔|件)/);
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

    function extractImage(el) {
        const imgWrap = el.querySelector('[class*="img"], [class*="Img"], [class*="pic"], [class*="Pic"], [class*="mainPic"], [class*="MainPic"]');
        if (imgWrap) {
            const img = imgWrap.querySelector('img');
            if (img) {
                const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
                if (src && !src.includes('spacer') && !src.includes('1x1')) return src.startsWith('//') ? 'https:' + src : src;
            }
        }
        for (const img of el.querySelectorAll('img')) {
            const w = parseInt(img.getAttribute('width') || img.naturalWidth || '0');
            const h = parseInt(img.getAttribute('height') || img.naturalHeight || '0');
            if (w > 0 && w < 50) continue;
            if (h > 0 && h < 50) continue;
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            if (src && !src.includes('spacer') && !src.includes('1x1') && !src.includes('tps-')) return src.startsWith('//') ? 'https:' + src : src;
        }
        return '';
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
            image: extractImage(card || a),
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
            cls.includes('disabled') || cls.includes('Disabled');
        return !disabled;
    });

    return {
        url: window.location.href,
        title: document.title,
        links,
        link_count: links.length,
        has_next: hasNext,
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
            cls.includes('disabled') || cls.includes('Disabled');
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


async def run_tmall_link_search(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    """按关键词采集天猫商品链接。"""
    params = task.params or {}
    keywords = _normalize_keywords(params.get("keywords")) or _DEFAULT_KEYWORDS
    raw_limit = params.get("limit")
    limit = min(int(raw_limit), _MAX_LIMIT) if raw_limit else 0
    max_pages = int(params.get("max_pages") or _DEFAULT_MAX_PAGES)
    pages_per_batch = int(params.get("pages_per_batch") or 2)

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

    logger.info(f"[tmall_link_search] Using context_key={ctx.context_id}")
    context_result = await agent_bay.context.get(ctx.context_id, create=False)
    if not context_result.success or not context_result.context:
        await task.fail(f"Context not found: {ctx.context_id}")
        return None

    await task.log_step(1, "创建浏览器会话", {"context_id": ctx.context_id}, {}, "running")
    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "task", "task_type": task.task_type, "task_id": str(task.id)},
            image_id=global_settings.agentbay.image_id,
            browser_context=AgentBayContext(context_result.context.id, auto_upload=False),
        )
    )
    if not session_result.success:
        await task.fail(f"Failed to create session: {session_result.error_message}")
        return None

    session = session_result.session
    browser = None
    step_counter = [2]

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
        browser_context.on("dialog", lambda dialog: dialog.dismiss())

        await task.log_step(1, "创建浏览器会话", {"context_id": ctx.context_id}, {"status": "ok"}, "completed")

        # 登录态校验
        agent = session.browser.agent
        login_result = await check_login_status(agent, "tmall")
        if login_result.logged_in:
            await task.log_step(step_counter[0], "登录态校验通过（天猫）", {"platform": "tmall"}, {"logged_in": True}, "completed")
        else:
            await task.log_step(step_counter[0], "登录态校验失败（天猫）", {"platform": "tmall"}, {"logged_in": False, "reason": login_result.reason}, "failed")
            await task.fail(login_failed_message("tmall"))
            return None
        step_counter[0] += 1

        # 断点续跑：加载已有链接，从断点页开始
        seen_urls: set[str] = set()
        existing_links = await ProductLink.filter(task_id=task.id)
        last_page = 0
        for link in existing_links:
            canonical = _canonicalize_product_url(link.url)
            if canonical:
                seen_urls.add(canonical)
            if link.page and link.page > last_page:
                last_page = link.page
        total_count = len(existing_links)
        remaining = max(0, (limit or _MAX_LIMIT) - total_count) if limit else _MAX_LIMIT
        if total_count:
            logger.info(f"[tmall_link_search] Resuming: {total_count} existing links, last page={last_page}, remaining={remaining}")
            task.progress = min(95, int(total_count / (limit or _MAX_LIMIT) * 100))
            task.result = {"total": total_count, "limit": limit or "不限", "keywords": keywords, "platform": "tmall"}
            await task.save()
        else:
            task.progress = 5
            await task.save()

        for index, keyword in enumerate(keywords):
            if limit and total_count >= limit:
                break

            remaining = (limit - total_count) if limit else _MAX_LIMIT
            kw_step = step_counter[0]
            step_counter[0] += 1
            await task.log_step(kw_step, f"采集天猫商品链接: {keyword}", {"keyword": keyword, "remaining": "不限" if not limit else remaining}, {}, "running")

            async def _on_page_done(page_items: list[dict], page_num: int):
                nonlocal total_count
                if page_items:
                    await ProductLink.upsert_bulk([
                        ProductLink(
                            task_id=task.id,
                            platform="tmall",
                            keyword=keyword,
                            page=page_num,
                            url=item.get("url", ""),
                            raw_url=item.get("raw_url", ""),
                            title=item.get("title", ""),
                            price=item.get("price", ""),
                            sales=item.get("sales", ""),
                            shop_name=item.get("shop", ""),
                            image=item.get("image", ""),
                            main_images=[item["image"]] if item.get("image") else [],
                            source_type=ProductLinkSourceType.KEYWORD_SEARCH.value,
                            monitor_status=ProductLinkMonitorStatus.CANDIDATE.value,
                        )
                        for item in page_items
                    ])
                    total_count += len(page_items)
                if limit:
                    task.progress = min(95, int(5 + total_count / limit * 90))
                else:
                    task.progress = min(95, 5 + page_num * 3)
                task.result = {
                    "total": total_count,
                    "limit": limit or "不限",
                    "keywords": keywords,
                    "platform": "tmall",
                }
                await task.save()
                s = step_counter[0]
                step_counter[0] += 1
                await task.log_step(
                    s, f"采集第{page_num}页: {keyword}",
                    {"keyword": keyword, "page": page_num},
                    {"new": len(page_items), "total": total_count},
                    "completed",
                )

            keyword_count = await _collect_keyword_links(
                session=session,
                browser_context=browser_context,
                keyword=keyword,
                remaining=remaining,
                max_pages=max_pages,
                global_seen=seen_urls,
                task_id=task.id,
                on_page_done=_on_page_done,
                start_page=last_page if total_count else 1,
                pages_per_batch=pages_per_batch,
            )

            await task.log_step(
                kw_step,
                f"采集天猫商品链接: {keyword}",
                {"keyword": keyword},
                {"found": keyword_count, "total": total_count},
                "completed",
            )
            step_counter[0] += 1

        all_links = await ProductLink.filter(task_id=task.id).order_by("created_at").limit(limit)
        link_urls = [item.url for item in all_links]
        output = json.dumps([item.to_dict() for item in all_links], ensure_ascii=False, indent=2)
        return {
            "links": link_urls,
            "total": len(link_urls),
            "limit": limit,
            "keywords": keywords,
            "platform": "tmall",
            "keyword_index": index,
            "keyword_count": keyword_count,
            "keyword_done": keyword_count < remaining,
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
            logger.warning(f"[tmall_link_search] Failed to close browser: {e}")

        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
        except Exception as e:
            logger.warning(f"[tmall_link_search] Failed to delete session: {e}")


async def _collect_keyword_links(
    session,
    browser_context,
    keyword: str,
    remaining: int,
    max_pages: int,
    global_seen: set[str],
    task_id=None,
    on_page_done=None,
    start_page: int = 1,
    pages_per_batch: int = 0,
) -> int:
    agent = session.browser.agent
    collected: list[dict[str, Any]] = []

    search_url = f"https://list.tmall.com/search_product.htm?q={quote(keyword)}&sort=sale-desc"
    await agent.navigate(search_url)
    await asyncio.sleep(5)
    await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")
    await _agent_act(agent, "点击销量进行销量排序。")

    # 断点续跑：直接点击目标页码
    if start_page > 1:
        await _agent_act(agent, f"滑动到最下方点击第{start_page}页按钮。")
        await asyncio.sleep(3)

    page = await _get_tmall_page(browser_context, search_url)
    page_index = start_page
    empty_pages = 0

    while page_index <= max_pages and len(collected) < remaining:
        if task_id:
            t = await Task.filter(id=task_id).first()
            if t and t.status == TaskStatus.CANCELLED.value:
                logger.info(f"[tmall_link_search] Task cancelled, stopping")
                break

        try:
            await _wait_search_page_ready(page)
            page_links = await _scroll_and_extract_page_links(agent, page, browser_context, search_url)
        except Exception as e:
            logger.error(f"[tmall_link_search] page.evaluate failed (transport closed?): {e}")
            break

        page_items = []
        for raw in page_links:
            canonical_url = _canonicalize_product_url(raw.get("url", ""))
            if not canonical_url or canonical_url in global_seen:
                continue
            global_seen.add(canonical_url)
            page_items.append(
                {
                    "keyword": keyword,
                    "page": page_index,
                    "url": canonical_url,
                    "raw_url": raw.get("url", ""),
                    "title": raw.get("title", ""),
                    "price": raw.get("price", ""),
                    "sales": raw.get("sales", ""),
                    "shop": raw.get("shop", ""),
                    "image": raw.get("image", ""),
                }
            )
            if len(collected) + len(page_items) >= remaining:
                break

        logger.info(
            f"[tmall_link_search] keyword={keyword} page={page_index} "
            f"new={len(page_items)} page_links={len(page_links)} total={len(collected) + len(page_items)}"
        )

        if not page_items:
            empty_pages += 1
        else:
            empty_pages = 0
            collected.extend(page_items)
        if on_page_done:
            await on_page_done(page_items, page_index)

        if len(collected) >= remaining:
            break
        if empty_pages >= 3:
            logger.info(f"[tmall_link_search] keyword={keyword} 3 consecutive empty pages, stopping")
            break

        # 分批断点：每 pages_per_batch 页暂停
        pages_in_batch = page_index - start_page + 1
        if pages_per_batch > 0 and pages_in_batch >= pages_per_batch:
            logger.info(f"[tmall_link_search] keyword={keyword} batch limit reached ({pages_per_batch} pages), pausing at page {page_index}")
            break

        moved = await _go_next_page(agent, page)
        if not moved:
            break

        page_index += 1
        await asyncio.sleep(4)
        page = await _get_tmall_page(browser_context, search_url)

    return len(collected)


async def _scroll_and_extract_page_links(agent, page, browser_context=None, fallback_url="") -> list[dict[str, Any]]:
    seen: set[str] = set()
    links: list[dict[str, Any]] = []

    # agent 滚到最底部加载全部商品
    await _agent_act(agent, "将页面直接滚动到最底部")
    await asyncio.sleep(3)

    # agent 操作后 page 引用可能失效，重新获取
    if browser_context:
        fresh = await _get_tmall_page(browser_context, fallback_url)
        if fresh:
            page = fresh

    # 提取所有商品链接
    state = await page.evaluate(_EXTRACT_PRODUCT_LINKS_JS)
    for item in state.get("links", []):
        url = item.get("url")
        if url and url not in seen:
            seen.add(url)
            links.append(item)

    return links


async def _go_next_page(agent, page) -> bool:
    try:
        if await _agent_act(agent, "点击搜索结果列表底部的下一页按钮，进入下一页"):
            return True
    except Exception as e:
        logger.warning(f"[tmall_link_search] agent.act next page failed: {e}")

    try:
        return bool(await page.evaluate(_CLICK_NEXT_PAGE_JS))
    except Exception as e:
        logger.warning(f"[tmall_link_search] JS next page click failed: {e}")
        return False


async def _agent_act(agent, instruction: str, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            ret = await agent.act(ActOptions(action=instruction))
            return bool(getattr(ret, "success", False))
        except Exception as e:
            logger.warning(f"[tmall_link_search] agent.act failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return False


async def _get_tmall_page(browser_context, fallback_url: str):
    await asyncio.sleep(1)
    for page in browser_context.pages:
        try:
            if page.is_closed():
                continue
            if "tmall.com" in page.url or "tmall.hk" in page.url or "taobao.com" in page.url:
                return page
        except Exception:
            continue

    page = await browser_context.new_page()
    await page.goto(fallback_url, timeout=60000, wait_until="domcontentloaded")
    await asyncio.sleep(3)
    return page


async def _wait_search_page_ready(page) -> None:
    try:
        await page.wait_for_function(
            """() => {
                const links = document.querySelectorAll('a[href*="item.htm"], a[href*="detail.tmall"]');
                const body = document.body ? document.body.innerText : '';
                return links.length > 0 || body.includes('没有找到') || body.includes('登录');
            }""",
            timeout=15000,
        )
    except Exception:
        logger.warning("[tmall_link_search] Search page data wait timed out, extracting current DOM")


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

    host = parsed.netloc.lower()
    if "tmall" not in host:
        return ""

    query = parse_qs(parsed.query)
    item_id = (query.get("id") or query.get("item_id") or [""])[0]
    if not item_id:
        return url

    return urlunparse(("https", "detail.tmall.com", "/item.htm", "", urlencode({"id": item_id}), ""))


async def _run_standalone() -> None:
    parser = argparse.ArgumentParser(description="Standalone Tmall product link search")
    parser.add_argument("keywords", nargs="*", default=_DEFAULT_KEYWORDS, help="搜索关键词")
    parser.add_argument("--context-key", default=os.getenv("TMALL_CONTEXT_KEY", _DEFAULT_CONTEXT_KEY))
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--max-pages", type=int, default=_DEFAULT_MAX_PAGES)
    parser.add_argument("--output", default="/tmp/tmall_links.json")
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
            labels={"app": "micro-sniper", "kind": "standalone", "script": "tmall_link_search"},
            image_id=global_settings.agentbay.image_id,
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
        browser_context.on("dialog", lambda dialog: dialog.dismiss())

        seen_urls: set[str] = set()
        results: list[dict[str, Any]] = []
        for keyword in keywords:
            if len(results) >= limit:
                break
            remaining = limit - len(results)
            print(f"\nSearching keyword={keyword}, remaining={remaining}")

            async def _collect(page_items, page_num):
                results.extend(page_items)

            count = await _collect_keyword_links(
                session=session,
                browser_context=browser_context,
                keyword=keyword,
                remaining=remaining,
                max_pages=args.max_pages,
                global_seen=seen_urls,
                on_page_done=_collect,
            )
            print(f"Found {count} new links, total={len(results)}")

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
