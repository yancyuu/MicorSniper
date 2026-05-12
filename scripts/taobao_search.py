# -*- coding: utf-8 -*-
"""淘宝关键词搜索脚本 - 搜索商品并提取详情（头图、价格、销量等）"""

import asyncio
import json
from typing import List, Dict, Any
from urllib.parse import quote

from agentbay import (
    AsyncAgentBay, CreateSessionParams,
    BrowserContext as AgentBayContext, BrowserOption,
    BrowserScreen, BrowserFingerprint,
    ActOptions,
)
from config.settings import global_settings
from models.task import Task
from models.context import BrowserContext
from models.product_detail import ProductDetail
from models.product_link import ProductLink, ProductLinkMonitorStatus, ProductLinkSourceType
from utils.logger import logger


# 搜索页 JS：提取当前页所有商品链接和基本信息
_EXTRACT_SEARCH_JS = """
() => {
    const items = [];
    const seen = new Set();

    // 方法1: 从所有包含 item.htm 的 a 标签提取
    const allLinks = document.querySelectorAll('a[href*="item.htm"], a[href*="item_o.htm"]');
    allLinks.forEach(a => {
        const href = a.href;
        if (!href || seen.has(href)) return;
        if (!href.includes('taobao.com') && !href.includes('tmall.com')) return;
        seen.add(href);

        // 向上找卡片容器
        const card = a.closest('[class*="Card"]') || a.closest('[class*="Content"]') || a.closest('[class*="cardWrapper"]') || a.parentElement;
        const titleEl = card.querySelector('[class*="Title"] span, [class*="title"] span');
        const priceEl = card.querySelector('[class*="Price--priceInt"], [class*="priceInt"]');
        const priceFloatEl = card.querySelector('[class*="Price--priceFloat"], [class*="priceFloat"]');
        const salesEl = card.querySelector('[class*="SalesPoint"] span, [class*="sellCount"], [class*="realSales"]');
        const shopEl = card.querySelector('[class*="ShopName"] span, [class*="shopName"]');
        const imgEl = card.querySelector('img[src*="alicdn"], img[src*="taobaocdn"]');

        items.push({
            url: href,
            title: titleEl ? titleEl.textContent.trim() : '',
            price: (priceEl ? priceEl.textContent.trim() : '') + (priceFloatEl ? priceFloatEl.textContent.trim() : ''),
            sales: salesEl ? salesEl.textContent.trim() : '',
            shop_name: shopEl ? shopEl.textContent.trim() : '',
            image: imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '',
        });
    });

    // 方法2: 从卡片容器提取（兜底）
    if (items.length === 0) {
        const cards = document.querySelectorAll(
            '[class*="Card--doubleCardWrapper"], ' +
            '[class*="Content--contentInner"], ' +
            '[class*="Card--cardWrapper"]'
        );
        cards.forEach(card => {
            const link = card.querySelector('a[href]');
            const href = link ? link.href : '';
            if (!href || seen.has(href)) return;
            seen.add(href);

            const titleEl = card.querySelector('[class*="Title"] span, [class*="title"]');
            const priceEl = card.querySelector('[class*="Price"] span, [class*="price"]');
            const salesEl = card.querySelector('[class*="Sales"] span, [class*="sell"]');
            const shopEl = card.querySelector('[class*="Shop"] span, [class*="shop"]');
            const imgEl = card.querySelector('img');

            items.push({
                url: href,
                title: titleEl ? titleEl.textContent.trim() : '',
                price: priceEl ? priceEl.textContent.trim() : '',
                sales: salesEl ? salesEl.textContent.trim() : '',
                shop_name: shopEl ? shopEl.textContent.trim() : '',
                image: imgEl ? (imgEl.getAttribute('src') || imgEl.getAttribute('data-src') || '') : '',
            });
        });
    }

    return items;
}
"""

# 详情页 JS：一次性提取所有数据
_EXTRACT_DETAIL_JS = """
() => {
    const r = { title: '', price: '', original_price: '', sales: '',
        main_images: [], shop_name: '', shop_url: '', location: '',
        detail_images: [], sku_info: [] };

    // 标题
    const titleEl = document.querySelector('[class*="Title--title"] span, ' +
        'h1[class*="title"], [data-spm="1000983"] span');
    if (titleEl) r.title = titleEl.textContent.trim();

    // 价格
    const priceInt = document.querySelector('[class*="Price--priceInt"]');
    const priceFloat = document.querySelector('[class*="Price--priceFloat"]');
    if (priceInt) r.price = priceInt.textContent.trim();
    if (priceFloat) r.price += priceFloat.textContent.trim();

    // 原价
    const origPrice = document.querySelector('[class*="Price--originalPrice"] span, ' +
        '[class*="originalPrice"]');
    if (origPrice) r.original_price = origPrice.textContent.trim();

    // 销量
    const salesEl = document.querySelector('[class*="SalesPoint--sales"] span, ' +
        '[class*="sellCount"], [class*="Counter--count"]');
    if (salesEl) r.sales = salesEl.textContent.trim();

    // 头图
    const mainImgs = document.querySelectorAll(
        '[class*="PicGallery--mainImage"] img, ' +
        '[class*="mainPic"] img, ' +
        '[class*="PicGallery"] img[src*="alicdn"]'
    );
    mainImgs.forEach(img => {
        const src = img.getAttribute('src') || img.getAttribute('data-src') || '';
        if (src && (src.includes('alicdn') || src.includes('taobaocdn'))) {
            const full = src.startsWith('//') ? 'https:' + src : src;
            if (!r.main_images.includes(full)) r.main_images.push(full);
        }
    });

    // 店铺
    const shopNameEl = document.querySelector('[class*="ShopName"] span, ' +
        '[class*="shopName"] a, [data-spm="1000985"] span');
    if (shopNameEl) {
        r.shop_name = shopNameEl.textContent.trim();
        const shopLink = shopNameEl.closest('a');
        if (shopLink) r.shop_url = shopLink.href;
    }

    // 发货地
    const locEl = document.querySelector('[class*="SendTo"] span, ' +
        '[class*="location"], [class*="areaName"]');
    if (locEl) r.location = locEl.textContent.trim();

    // 尝试从 window.__INITIAL_DATA__ 提取更完整的数据
    try {
        if (window.__INITIAL_DATA__) {
            const d = window.__INITIAL_DATA__;
            if (!r.title && d.item && d.item.title) r.title = d.item.title;
            if (d.item && d.item.priceInfo) {
                if (!r.price && d.item.priceInfo.price) r.price = d.item.priceInfo.price;
                if (!r.original_price && d.item.priceInfo.originalPrice)
                    r.original_price = d.item.priceInfo.originalPrice;
            }
            if (d.item && d.item.sellCount && !r.sales) r.sales = String(d.item.sellCount);
            if (d.item && d.item.images && d.item.images.length && !r.main_images.length) {
                r.main_images = d.item.images.map(u => u.startsWith('//') ? 'https:' + u : u);
            }
            if (d.seller && d.seller.shopName && !r.shop_name) r.shop_name = d.seller.shopName;
            if (d.seller && d.seller.shopUrl && !r.shop_url) r.shop_url = d.seller.shopUrl;
            if (d.item && d.item.descUrl) r.detail_images = [d.item.descUrl];
            if (d.item && d.item.desc) r.detail_desc = d.item.desc;
        }
    } catch(e) {}

    // 尝试从 window.g_config 提取
    try {
        if (window.g_config && window.g_config.idata) {
            const id = window.g_config.idata;
            if (!r.title && id.item && id.item.title) r.title = id.item.title;
            if (id.item && id.item.price && !r.price) r.price = id.item.price;
        }
    } catch(e) {}

    // SKU
    try {
        const skuEls = document.querySelectorAll('[class*="skuItem"], [class*="SKUItem"]');
        skuEls.forEach(el => {
            const text = el.textContent.trim();
            if (text) r.sku_info.push(text);
        });
    } catch(e) {}

    return r;
}
"""

# 关闭弹窗 JS
_DISMISS_POPUP_JS = """
() => {
    const close = document.querySelector('[class*="modal"] [class*="close"], ' +
        '[class*="Dialog"] [class*="close"], ' +
        '[class*="login"] [class*="close"], ' +
        '.tb-close, .close-btn, [data-spm="close"]');
    if (close) close.click();
}
"""


async def run_taobao_search(task: Task, ctx: BrowserContext):
    """淘宝关键词搜索主函数"""
    params = task.params or {}
    keywords = params.get("keywords", [])
    limit = params.get("limit") or 0  # 0 表示不限

    if not keywords:
        await task.fail("No keywords provided")
        return

    from sanic import Sanic
    app = Sanic.get_app()
    playwright = app.ctx.playwright

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    context_key = ctx.context_id
    if not context_key:
        await task.fail("Context has no context_id, please login first")
        return

    logger.info(f"[taobao_search] Using context_key={context_key}")
    context_result = await agent_bay.context.get(context_key, create=False)
    if not context_result.success or not context_result.context:
        await task.fail(f"Context not found: {context_key}")
        return

    logger.info(f"[taobao_search] Got AgentBay context, internal_id={context_result.context.id}")

    await task.log_step(1, "创建浏览器会话", {"context_id": context_key}, {}, "running")

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "task", "task_type": task.task_type, "task_id": str(task.id)},
            image_id=global_settings.agentbay.image_id,
            browser_context=AgentBayContext(context_result.context.id, auto_upload=True)
        )
    )
    if not session_result.success:
        await task.fail(f"Failed to create session: {session_result.error_message}")
        return

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
            return

        endpoint_url = await session.browser.get_endpoint_url()
        browser = await playwright.chromium.connect_over_cdp(endpoint_url)
        context = browser.contexts[0] if browser.contexts else await browser.new_context()

        await task.log_step(1, "创建浏览器会话", {"context_id": context_key}, {"status": "ok"}, "completed")
        task.progress = 5
        await task.save()

        # ========== 阶段一：搜索 & 收集链接（每页写 ProductLink） ==========
        total_count = 0
        seen_urls = set()

        for i, keyword in enumerate(keywords):
            remaining = (limit - total_count) if limit else 0

            await task.log_step(
                i + 2, f"搜索关键词: {keyword}",
                {"keyword": keyword, "limit": limit or "不限"}, {}, "running"
            )

            items = await _search_keyword(
                session, context, keyword, remaining,
                task=task, seen_urls=seen_urls,
                total_count_before=total_count,
                limit=limit, keyword_index=i, keywords_total=len(keywords),
            )
            total_count += len(items)

            await task.log_step(
                i + 2, f"搜索关键词: {keyword}",
                {"keyword": keyword},
                {"found": len(items), "total": total_count},
                "completed"
            )

            if limit and total_count >= limit:
                logger.info(f"[taobao] Reached limit {limit}, stopping search")
                break

        # ========== 阶段二：详情提取 ==========
        search_links = await ProductLink.filter(task_id=task.id).order_by("created_at")
        if limit:
            search_links = search_links[:limit]

        detail_count = 0
        step_base = len(keywords) + 2

        if search_links:
            await task.log_step(
                step_base, "获取商品详情",
                {"count": len(search_links)}, {}, "running"
            )

            page = await context.new_page()
            try:
                for j, link in enumerate(search_links):
                    detail = await _extract_detail(page, link.url)

                    if detail.get("title"):
                        link.title = detail["title"]
                    if detail.get("price"):
                        link.price = detail["price"]
                    if detail.get("sales"):
                        link.sales = detail["sales"]
                    if detail.get("shop_name"):
                        link.shop_name = detail["shop_name"]
                    await link.save()
                    await ProductDetail.upsert_from_info(
                        task_id=task.id,
                        product_link_id=link.id,
                        platform="taobao",
                        url=link.url,
                        info=detail,
                    )

                    detail_count += 1
                    # progress: 详情阶段 35-95%
                    progress = int(35 + detail_count / len(search_links) * 60)
                    task.progress = min(progress, 95)

                    if j % 5 == 0 or j == len(search_links) - 1:
                        task.result = {
                            "total": detail_count,
                            "keywords": keywords,
                            "platform": "taobao",
                        }
                        await task.save()
                        logger.info(f"[taobao] Detail {j+1}/{len(search_links)}: {link.title[:30]}")
            finally:
                await page.close()

            await task.log_step(
                step_base, "获取商品详情",
                {"count": len(search_links)},
                {"details_found": detail_count},
                "completed"
            )

        return {
            "total": detail_count or total_count,
            "keywords": keywords,
            "platform": "taobao",
        }

    finally:
        try:
            if browser:
                cdp = await browser.new_browser_cdp_session()
                await cdp.send('Browser.close')
                await asyncio.sleep(0.5)
                await browser.close()
        except:
            pass
        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=True), timeout=30)
        except Exception as e:
            logger.warning(f"[taobao] Failed to delete session: {e}")


async def _search_keyword(
    session, context, keyword: str, remaining: int,
    task=None, seen_urls: set = None, total_count_before: int = 0,
    limit: int = 0, keyword_index: int = 0, keywords_total: int = 1,
) -> List[Dict]:
    """搜索关键词，滚动加载，JS 提取商品链接。

    如果传了 task，每次滚动后自动写 ProductLink 表并更新 progress。
    如果不传 task，只返回结果列表（兼容 standalone 模式）。
    """
    encoded_kw = quote(keyword)
    url = f"https://s.taobao.com/search?q={encoded_kw}&sort=sale-desc"

    agent = session.browser.agent
    try:
        await agent.navigate(url)
    except Exception:
        logger.warning(f"[taobao] agent.navigate failed, falling back to CDP")
    await asyncio.sleep(5)

    try:
        ret = await agent.act(ActOptions(action="关闭页面上所有弹框、登录提示、广告弹窗"))
        if getattr(ret, "success", False):
            logger.info(f"[taobao] Popups dismissed by agent.act")
    except Exception as e:
        logger.warning(f"[taobao] agent.act dismiss failed: {e}")
    await asyncio.sleep(3)

    page = None
    for p in context.pages:
        if 'taobao.com' in p.url:
            page = p
            break
    if not page:
        page = await context.new_page()
        await page.goto(url, timeout=60000, wait_until="domcontentloaded")
        await asyncio.sleep(3)

    results = []

    try:
        try:
            await page.wait_for_function(
                """() => {
                    const cards = document.querySelectorAll(
                        '[class*="Card--doubleCardWrapper"] a, ' +
                        '[class*="Content--contentInner"] a, ' +
                        '[class*="MainPic--mainPic"] img'
                    );
                    return cards.length > 0;
                }""",
                timeout=15000
            )
        except:
            logger.warning(f"[taobao] Wait for data timed out for '{keyword}', trying anyway...")

        max_scrolls = 50
        for scroll_round in range(max_scrolls):
            items = await page.evaluate(_EXTRACT_SEARCH_JS)

            new_items = []
            seen = {r["url"] for r in results}
            for it in items:
                u = it.get("url", "")
                if u and u not in seen:
                    # 如果有全局去重集合，也检查
                    if seen_urls and u in seen_urls:
                        continue
                    seen.add(u)
                    if seen_urls:
                        seen_urls.add(u)
                    new_items.append(it)

            # 每次滚动后写 ProductLink
            if task and new_items:
                await ProductLink.upsert_bulk([
                    ProductLink(
                        task_id=task.id,
                        platform="taobao",
                        keyword=keyword,
                        url=it.get("url", ""),
                        title=it.get("title", ""),
                        price=it.get("price", ""),
                        sales=it.get("sales", ""),
                        shop_name=it.get("shop_name", ""),
                        image=it.get("image", ""),
                        source_type=ProductLinkSourceType.KEYWORD_SEARCH.value,
                        monitor_status=ProductLinkMonitorStatus.CANDIDATE.value,
                    )
                    for it in new_items
                ])
                # 每轮滚动打一条日志
                await task.log_step(
                    len(task.logs), f"滚动采集第{scroll_round+1}轮: {keyword}",
                    {"keyword": keyword, "scroll_round": scroll_round + 1},
                    {"new": len(new_items), "total": total_so_far},
                    "completed",
                )

            results.extend(new_items)
            total_so_far = total_count_before + len(results)

            # 更新 progress
            if task:
                if limit:
                    task.progress = min(35, int(5 + total_so_far / limit * 30))
                else:
                    task.progress = min(35, int(5 + (keyword_index + scroll_round / max_scrolls) / keywords_total * 30))
                task.result = {
                    "total": total_so_far,
                    "limit": limit or "不限",
                    "keywords": [k for k in task.params.get("keywords", [])],
                    "platform": "taobao",
                }
                await task.save()

            logger.info(f"[taobao] Scroll {scroll_round+1}: got {len(new_items)} new, total {len(results)}")

            if remaining and len(results) >= remaining:
                break
            if not new_items and scroll_round > 2:
                break

            await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await asyncio.sleep(1.5)

    except Exception as e:
        logger.error(f"[taobao] Search failed for '{keyword}': {e}")
    finally:
        try:
            await page.close()
        except Exception:
            pass

    return results


async def _extract_detail(page, url: str) -> Dict:
    """串行访问商品详情页，JS 并行提取数据"""
    try:
        await page.goto(url, timeout=30000, wait_until="domcontentloaded")
        await asyncio.sleep(1.5)

        try:
            await page.evaluate(_DISMISS_POPUP_JS)
        except:
            pass

        data = await page.evaluate(_EXTRACT_DETAIL_JS)
        return data or {}

    except Exception as e:
        logger.warning(f"[taobao] Detail extraction failed for {url}: {e}")
        return {}
