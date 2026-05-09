# -*- coding: utf-8 -*-
"""京东关键词搜索链接采集任务。

只负责通过关键词搜索京东商品链接，不进入详情页。页面动作使用 AgentBay agent，
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


_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 1000
_DEFAULT_MAX_PAGES = 100
_DEFAULT_KEYWORDS = ["SKG"]
_DEFAULT_CONTEXT_KEY = "jd-context:default:placeholder"
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
            return host.endsWith('jd.com') && (path.includes('/item') || path.match(/\\/\\d+\\.html/));
        } catch (e) {
            return false;
        }
    }

    function extractPrice(el) {
        const priceContainer = el.querySelector('[class*=\"_price_\"]');
        if (priceContainer) {
            const spans = priceContainer.querySelectorAll('span');
            for (const sp of spans) {
                const m = sp.textContent.match(/[\\d,.]+/);
                if (m) return m[0];
            }
        }
        const text = el.textContent || '';
        const m = text.match(/¥\\s*([\\d,.]+)/);
        return m ? m[1] : '';
    }

    function extractSales(el) {
        const text = el.textContent || '';
        const m = text.match(/(\\d[\\d,.]*万?\\+?)\\s*(人浏览|人关注|条评价|个评价|万\\+?评价)/);
        return m ? m[1] + m[2] : '';
    }

    function extractShop(el) {
        const shopEl = el.querySelector('[class*=\"_shopName\"], [class*=\"_storeName\"], [class*=\"shopName\"]');
        if (shopEl) return shopEl.textContent.trim().slice(0, 60);
        const selfTag = el.querySelector('img[alt=\"自营\"], [class*=\"_tag_\"] img');
        if (selfTag) return '京东自营';
        return '';
    }

    function extractTitle(el) {
        const titleEl = el.querySelector('[class*=\"_goods_title\"], [class*=\"_title_\"] span[title], span[title]');
        if (titleEl) return (titleEl.getAttribute('title') || titleEl.textContent).trim().slice(0, 120);
        const titleEl2 = el.querySelector('[class*=\"_newStyle_\"]');
        return titleEl2 ? titleEl2.textContent.trim().slice(0, 120) : '';
    }

    function extractImage(el) {
        const img = el.querySelector('img[data-src]');
        if (img) {
            const src = img.getAttribute('data-src') || '';
            if (src && !src.includes('spacer') && !src.includes('loading')) return src.startsWith('//') ? 'https:' + src : src;
        }
        for (const img of el.querySelectorAll('img')) {
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            if (src.includes('360buyimg.com/n2/s') || src.includes('360buyimg.com/n1')) return src.startsWith('//') ? 'https:' + src : src;
        }
        return '';
    }

    // 新版京东搜索：商品卡片在 div[data-sku] 中
    for (const el of document.querySelectorAll('div[data-sku]')) {
        const sku = el.getAttribute('data-sku');
        if (!sku || seen.has(sku)) continue;
        // 跳过广告/推荐区域
        const parent = el.closest('[class*="recommend"], [class*="Recommend"], [class*="ad"], [class*="banner"]');
        if (parent) continue;
        seen.add(sku);

        const url = 'https://item.jd.com/' + sku + '.html';
        links.push({
            url,
            title: extractTitle(el) || (el.textContent || '').trim().slice(0, 80),
            price: extractPrice(el),
            sales: extractSales(el),
            shop: extractShop(el),
            image: extractImage(el),
            card_text: (el.textContent || '').trim().slice(0, 200),
        });
    }

    // 兼容旧版：li.gl-item[data-sku]
    const mainList = document.querySelector('#J_goodsList');
    if (mainList) {
        for (const el of mainList.querySelectorAll('li.gl-item[data-sku]')) {
            const sku = el.getAttribute('data-sku');
            if (!sku || seen.has(sku)) continue;
            seen.add(sku);
            links.push({
                url: 'https://item.jd.com/' + sku + '.html',
                title: extractTitle(el) || (el.textContent || '').trim().slice(0, 80),
                price: extractPrice(el),
                sales: extractSales(el),
                shop: extractShop(el),
                image: extractImage(el),
                card_text: (el.textContent || '').trim().slice(0, 200),
            });
        }
    }

    const nextCandidates = Array.from(document.querySelectorAll('a, button, span')).filter((el) => {
        const text = (el.textContent || '').trim();
        const cls = el.className ? String(el.className) : '';
        return text === '下一页' || cls.includes('next') || cls.includes('Next') || cls.includes('pn-next');
    });
    const hasNext = nextCandidates.some((el) => {
        const cls = el.className ? String(el.className) : '';
        const disabled = el.getAttribute('disabled') !== null ||
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



async def run_jd_link_search(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    """按关键词采集京东商品链接。"""
    params = task.params or {}
    keywords = _normalize_keywords(params.get("keywords")) or _DEFAULT_KEYWORDS
    raw_limit = params.get("limit")
    limit = min(int(raw_limit), _MAX_LIMIT) if raw_limit else 0
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

    logger.info(f"[jd_link_search] Using context_key={ctx.context_id}")
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
        # 自动 dismiss JS 弹窗，防止 Playwright 处理 dialog 时与 agent 冲突导致进程崩溃
        browser_context.on("dialog", lambda dialog: dialog.dismiss())

        await task.log_step(1, "创建浏览器会话", {"context_id": ctx.context_id}, {"status": "ok"}, "completed")

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
            logger.info(f"[jd_link_search] Resuming: {total_count} existing links, last page={last_page}, remaining={remaining}")
            task.progress = min(95, int(total_count / (limit or _MAX_LIMIT) * 100))
            task.result = {"total": total_count, "limit": limit or "不限", "keywords": keywords, "platform": "jd"}
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
            await task.log_step(kw_step, f"采集京东商品链接: {keyword}", {"keyword": keyword, "remaining": "不限" if not limit else remaining}, {}, "running")

            async def _on_page_done(page_items: list[dict], page_num: int):
                nonlocal total_count
                if page_items:
                    await ProductLink.upsert_bulk([
                        ProductLink(
                            task_id=task.id,
                            platform="jd",
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
                    "platform": "jd",
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
            )

            await task.log_step(
                kw_step,
                f"采集京东商品链接: {keyword}",
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
            logger.warning(f"[jd_link_search] Failed to close browser: {e}")

        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
        except Exception as e:
            logger.warning(f"[jd_link_search] Failed to delete session: {e}")


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
) -> int:
    agent = session.browser.agent
    collected: list[dict[str, Any]] = []

    search_url = f"https://search.jd.com/Search?keyword={quote(keyword)}"
    await agent.navigate(search_url)
    await asyncio.sleep(5)
    await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")
    # 销量排序：先点销量tab，等页面刷新
    for _attempt in range(3):
        ok = await _agent_act(agent, "点击搜索结果顶部的'销量'排序按钮")
        if ok:
            await asyncio.sleep(5)
            break
        await asyncio.sleep(2)

    page = await _get_jd_page(browser_context, search_url)
    page_index = 1
    empty_pages = 0

    while page_index <= max_pages and len(collected) < remaining:
        if task_id:
            t = await Task.filter(id=task_id).first()
            if t and t.status == TaskStatus.CANCELLED.value:
                logger.info(f"[jd_link_search] Task cancelled, stopping")
                break

        try:
            await _wait_search_page_ready(page)
            page_links = await _scroll_and_extract_page_links(page, global_seen)
        except Exception as e:
            logger.error(f"[jd_link_search] page.evaluate failed (transport closed?): {e}")
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
            f"[jd_link_search] keyword={keyword} page={page_index} "
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
            logger.info(f"[jd_link_search] keyword={keyword} 3 consecutive empty pages, stopping")
            break

        page_index += 1
        # 京东分页在页面中间，用 JS 找"下一页"文本点击
        try:
            await page.evaluate("""
            () => {
                const els = Array.from(document.querySelectorAll('a, span, div'));
                const next = els.find(el => (el.textContent || '').trim() === '下一页');
                if (next) { next.scrollIntoView({block: 'center'}); next.click(); return true; }
                return false;
            }
            """)
            await asyncio.sleep(5)
        except Exception as e:
            logger.warning(f"[jd_link_search] JS next page click failed: {e}")
            # fallback: 滚动到底部触发加载
            try:
                await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight)")
                await asyncio.sleep(4)
            except Exception:
                pass
        page = await _get_jd_page(browser_context, search_url)

    return len(collected)


async def _scroll_and_extract_page_links(page, seen_skus: set[str] | None = None) -> list[dict[str, Any]]:
    seen: set[str] = set()
    links: list[dict[str, Any]] = []

    # 先滚动几下触发懒加载
    for _ in range(3):
        await page.evaluate("window.scrollTo(0, document.documentElement.scrollHeight || document.body.scrollHeight)")
        await asyncio.sleep(1.2)

    # 只提取未见过的 SKU
    skip_list = list(seen_skus) if seen_skus else []
    state = await page.evaluate(
        """({skus}) => { const skip = new Set(skus); const seen = new Set(); const links = [];
        for (const el of document.querySelectorAll('div[data-sku]')) {
            const sku = el.getAttribute('data-sku');
            if (!sku || seen.has(sku) || skip.has(sku)) continue;
            if (el.closest('[class*=recommend],[class*=ad],[class*=banner]')) continue;
            seen.add(sku);
            const img = el.querySelector('img[class*="_img_"]');
            let image = '';
            if (img) { const s = img.getAttribute('src') || img.getAttribute('data-src') || ''; image = s.startsWith('//') ? 'https:'+s : s; }
            links.push({url:'https://item.jd.com/'+sku+'.html', sku, title: el.textContent.trim().slice(0,80), image});
        } return {links}; }""",
        skip_list,
    )
    for item in state.get("links", []):
        url = item.get("url")
        if url and url not in seen:
            seen.add(url)
            links.append(item)

    return links



async def _agent_act(agent, instruction: str, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            ret = await agent.act(ActOptions(action=instruction))
            return bool(getattr(ret, "success", False))
        except Exception as e:
            logger.warning(f"[jd_link_search] agent.act failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return False


async def _get_jd_page(browser_context, fallback_url: str):
    # 等页面稳定后再获取引用
    await asyncio.sleep(1)
    try:
        for page in browser_context.pages:
            try:
                if page.is_closed():
                    continue
                if "jd.com" in page.url:
                    return page
            except Exception:
                continue
    except Exception:
        logger.warning("[jd_link_search] browser_context.pages failed, context may be closed")

    try:
        page = await browser_context.new_page()
        await page.goto(fallback_url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(3)
        return page
    except Exception as e:
        logger.error(f"[jd_link_search] Failed to create new page: {e}")
        raise


async def _wait_search_page_ready(page) -> None:
    try:
        await page.wait_for_function(
            """() => {
                const skus = document.querySelectorAll('[data-sku]');
                const body = document.body ? document.body.innerText : '';
                return skus.length > 0 || body.includes('没有找到') || body.includes('登录');
            }""",
            timeout=15000,
        )
    except Exception:
        logger.warning("[jd_link_search] Search page data wait timed out, extracting current DOM")


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

    # JD 商品 URL 格式: item.jd.com/12345.html 或 item.jd.com/#12345
    host = parsed.netloc.lower()
    if "jd.com" not in host:
        return ""

    # 从 path 提取商品 ID
    path = parsed.path
    import re
    m = re.search(r'/(\d+)\.html', path)
    if m:
        item_id = m.group(1)
    else:
        query = parse_qs(parsed.query)
        item_id = (query.get("sku") or query.get("id") or [""])[0]
        if not item_id:
            # 从 hash 提取
            if parsed.fragment and parsed.fragment.isdigit():
                item_id = parsed.fragment
            else:
                return url  # 无法提取 ID，返回原始 URL

    return f"https://item.jd.com/{item_id}.html"


async def _run_standalone() -> None:
    parser = argparse.ArgumentParser(description="Standalone JD product link search")
    parser.add_argument("keywords", nargs="*", default=_DEFAULT_KEYWORDS, help="搜索关键词")
    parser.add_argument("--context-key", default=os.getenv("JD_CONTEXT_KEY", _DEFAULT_CONTEXT_KEY))
    parser.add_argument("--limit", type=int, default=_DEFAULT_LIMIT)
    parser.add_argument("--max-pages", type=int, default=_DEFAULT_MAX_PAGES)
    parser.add_argument("--output", default="/tmp/jd_links.json")
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
            labels={"app": "micro-sniper", "kind": "standalone", "script": "jd_link_search"},
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
        # 自动 dismiss JS 弹窗，防止 Playwright 处理 dialog 时与 agent 冲突导致进程崩溃
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
