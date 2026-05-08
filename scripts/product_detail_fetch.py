# -*- coding: utf-8 -*-
"""商品详情抓取：打开商品链接，提取完整商品信息，更新到 ProductLink。

用法:
  poetry run python -m scripts.product_detail_fetch --urls "url1,url2" --context-key "xxx"
"""

import asyncio
import argparse
import json
from typing import Any

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
from models.product_link import ProductLink
from utils.logger import logger


async def _agent_act(agent, instruction: str, retries: int = 2) -> bool:
    for attempt in range(retries):
        try:
            ret = await agent.act(ActOptions(action=instruction))
            return bool(getattr(ret, "success", False))
        except Exception as e:
            logger.warning(f"[product_detail_fetch] agent.act failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return False


# ── 平台检测 ──

_DETECT_PLATFORM_JS = """
() => {
    const host = window.location.hostname;
    if (host.includes('jd.com')) return 'jd';
    if (host.includes('tmall.com') || host.includes('tmall.hk')) return 'tmall';
    if (host.includes('taobao.com')) return 'taobao';
    if (host.includes('1688.com')) return '1688';
    return 'unknown';
}
"""

# ── 淘宝详情页提取 ──

_TAOBAO_DETAIL_JS = """
() => {
    const result = {};

    // 标题
    const titleEl = document.querySelector('[class*="Title--"], [class*="title--"], h1, [data-spm="1000983"]');
    result.title = titleEl ? titleEl.textContent.trim().slice(0, 500) : '';

    // 价格
    const priceEl = document.querySelector('[class*="priceText"], [class*="Price--priceText"], [class*="priceText--"]');
    result.price = priceEl ? priceEl.textContent.replace(/[^\\d.]/g, '') : '';
    if (!result.price) {
        const m = document.body.innerText.match(/¥\\s*([\\d,.]+)/);
        result.price = m ? m[1] : '';
    }

    // 原价
    const origEl = document.querySelector('[class*="originalPrice"], [class*="Price--original"]');
    result.original_price = origEl ? origEl.textContent.replace(/[^\\d.]/g, '') : '';

    // 销量
    const salesEl = document.querySelector('[class*="soldContent"], [class*="Sold--"], [class*="sale"]');
    result.sales = salesEl ? salesEl.textContent.trim() : '';
    if (!result.sales) {
        const m = document.body.innerText.match(/(\\d[\\d,]*\\+?)\\s*(人付款|人收货|月销|已售)/);
        result.sales = m ? m[1] + m[2] : '';
    }

    // 店铺
    const shopEl = document.querySelector('[class*="shopName"], [class*="ShopName--"], [class*="shopNameText"]');
    result.shop_name = shopEl ? shopEl.textContent.trim().slice(0, 200) : '';
    const shopLink = document.querySelector('[class*="shopName"] a, [class*="ShopName"] a, a[href*="shop"]');
    result.shop_url = shopLink ? shopLink.href : '';

    // 主图
    const mainImages = [];
    document.querySelectorAll('[class*="mainPic"], [class*="PicGallery--"] img, [class*="main-image"] img').forEach(img => {
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && !src.includes('spacer') && !src.includes('1x1')) {
            const full = src.startsWith('//') ? 'https:' + src : src;
            if (!mainImages.includes(full)) mainImages.push(full);
        }
    });
    // 兜底：取第一张大图
    if (mainImages.length === 0) {
        for (const img of document.querySelectorAll('img')) {
            const src = (img.getAttribute('data-src') || img.getAttribute('src') || '');
            if (src.includes('imgextra') || src.includes('taobaocdn') || src.includes('alicdn')) {
                const full = src.startsWith('//') ? 'https:' + src : src;
                if (!mainImages.includes(full)) mainImages.push(full);
            }
        }
    }
    result.image = mainImages[0] || '';
    result.main_images = mainImages;

    // SKU
    const skuItems = [];
    document.querySelectorAll('[class*="skuItem"], [class*="SKUItem"], [class*="sku-item"]').forEach(el => {
        skuItems.push(el.textContent.trim());
    });
    result.sku_info = skuItems;

    // 发货地
    const locEl = document.querySelector('[class*="locText"], [class*="Loc--"], [class*="shipAddress"]');
    result.location = locEl ? locEl.textContent.trim() : '';

    // 详情图
    const detailImages = [];
    document.querySelectorAll('#description img, [class*="desc"] img, [id*="desc"] img').forEach(img => {
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && src.startsWith('http')) {
            detailImages.push(src);
        }
    });
    result.detail_images = detailImages;

    return result;
}
"""

# ── 天猫详情页提取（和淘宝结构类似但选择器有差异） ──

_TMALL_DETAIL_JS = """
() => {
    const result = {};

    // 标题
    const titleEl = document.querySelector('[class*="ItemHeader--"], [class*="title--"], h1');
    result.title = titleEl ? titleEl.textContent.trim().slice(0, 500) : '';

    // 价格
    const priceEl = document.querySelector('[class*="priceText"], [class*="Price--priceText"], [class*="tm-price"]');
    result.price = priceEl ? priceEl.textContent.replace(/[^\\d.]/g, '') : '';
    if (!result.price) {
        const m = document.body.innerText.match(/¥\\s*([\\d,.]+)/);
        result.price = m ? m[1] : '';
    }

    // 原价
    const origEl = document.querySelector('[class*="originalPrice"], [class*="Price--original"]');
    result.original_price = origEl ? origEl.textContent.replace(/[^\\d.]/g, '') : '';

    // 销量
    const salesEl = document.querySelector('[class*="Sold--"], [class*="soldContent"], [class*="tm-count"]');
    result.sales = salesEl ? salesEl.textContent.trim() : '';
    if (!result.sales) {
        const m = document.body.innerText.match(/(\\d[\\d,]*\\+?)\\s*(人付款|人收货|月销|已售)/);
        result.sales = m ? m[1] + m[2] : '';
    }

    // 店铺
    const shopEl = document.querySelector('[class*="shopName"], [class*="ShopName--"]');
    result.shop_name = shopEl ? shopEl.textContent.trim().slice(0, 200) : '';
    const shopLink = document.querySelector('a[href*="shop"], a[href*="store"]');
    result.shop_url = shopLink ? shopLink.href : '';

    // 主图
    const mainImages = [];
    document.querySelectorAll('[class*="PicGallery--"] img, [class*="mainPic"] img').forEach(img => {
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && !src.includes('spacer') && !src.includes('1x1')) {
            const full = src.startsWith('//') ? 'https:' + src : src;
            if (!mainImages.includes(full)) mainImages.push(full);
        }
    });
    if (mainImages.length === 0) {
        for (const img of document.querySelectorAll('img')) {
            const src = (img.getAttribute('data-src') || img.getAttribute('src') || '');
            if (src.includes('imgextra') || src.includes('alicdn')) {
                const full = src.startsWith('//') ? 'https:' + src : src;
                if (!mainImages.includes(full)) mainImages.push(full);
            }
        }
    }
    result.image = mainImages[0] || '';
    result.main_images = mainImages;

    // SKU
    const skuItems = [];
    document.querySelectorAll('[class*="skuItem"], [class*="SKUItem"]').forEach(el => {
        skuItems.push(el.textContent.trim());
    });
    result.sku_info = skuItems;

    // 发货地
    const locEl = document.querySelector('[class*="locText"], [class*="Loc--"]');
    result.location = locEl ? locEl.textContent.trim() : '';

    // 详情图
    const detailImages = [];
    document.querySelectorAll('#description img, [class*="desc"] img').forEach(img => {
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && src.startsWith('http')) detailImages.push(src);
    });
    result.detail_images = detailImages;

    return result;
}
"""

# ── 京东详情页提取 ──

_JD_DETAIL_JS = """
() => {
    const result = {};

    // 标题
    const titleEl = document.querySelector('[class*="itemInfo"], .itemInfo-wrap .sku-name, [class*="product-name"]');
    result.title = titleEl ? titleEl.textContent.trim().slice(0, 500) : '';

    // 价格
    const priceEl = document.querySelector('[class*="price"], .p-price .price, [class*="J-p-"]');
    result.price = priceEl ? priceEl.textContent.replace(/[^\\d.]/g, '') : '';
    if (!result.price) {
        const m = document.body.innerText.match(/¥\\s*([\\d,.]+)/);
        result.price = m ? m[1] : '';
    }

    // 原价
    const origEl = document.querySelector('.p-price del, [class*="orig-price"]');
    result.original_price = origEl ? origEl.textContent.replace(/[^\\d.]/g, '') : '';

    // 销量
    result.sales = '';
    const m = document.body.innerText.match(/(\\d[\\d,]*\\+?)\\s*(万)?\\s*(人?评价|人?购买|人?收货)/);
    if (m) result.sales = m[0];

    // 店铺
    const shopEl = document.querySelector('[class*="shopName"], .J-hove-wrap .name a, [class*="shopName"] a');
    result.shop_name = shopEl ? shopEl.textContent.trim().slice(0, 200) : '';
    const shopLink = document.querySelector('[class*="shopName"] a, a[href*="shop.jd"], a[href*="mall.jd"]');
    result.shop_url = shopLink ? shopLink.href : '';

    // 主图
    const mainImages = [];
    document.querySelectorAll('#spec-img, [class*="main-img"] img, [class*="J-detail-content"] img, .lh img').forEach(img => {
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && !src.includes('spacer') && !src.includes('1x1')) {
            const full = src.startsWith('//') ? 'https:' + src : src;
            if (!mainImages.includes(full)) mainImages.push(full);
        }
    });
    result.image = mainImages[0] || '';
    result.main_images = mainImages;

    // SKU
    const skuItems = [];
    document.querySelectorAll('[class*="sku-item"], #choose-attr .item a, [class*="J-sku-item"]').forEach(el => {
        skuItems.push(el.textContent.trim());
    });
    result.sku_info = skuItems;

    // 发货地
    result.location = '';

    // 详情图
    const detailImages = [];
    document.querySelectorAll('#detail .detail-content img, [class*="detail-list"] img, #J-detail-content img').forEach(img => {
        const src = img.getAttribute('data-lazyload') || img.getAttribute('data-src') || img.getAttribute('src') || '';
        if (src && src.startsWith('http')) detailImages.push(src);
    });
    result.detail_images = detailImages;

    return result;
}
"""

# ── 通用兜底提取 ──

_GENERIC_DETAIL_JS = """
() => {
    const result = {};
    result.title = document.title || '';
    const m = document.body.innerText.match(/¥\\s*([\\d,.]+)/);
    result.price = m ? m[1] : '';
    result.original_price = '';
    result.sales = '';
    result.shop_name = '';
    result.shop_url = '';
    result.image = '';
    result.main_images = [];
    result.sku_info = [];
    result.location = '';
    result.detail_images = [];
    return result;
}
"""

_PLATFORM_DETAIL_JS = {
    "taobao": _TAOBAO_DETAIL_JS,
    "tmall": _TMALL_DETAIL_JS,
    "jd": _JD_DETAIL_JS,
}


async def run_product_detail_fetch(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    """打开商品链接，提取完整商品信息，更新到 ProductLink。"""
    params = task.params or {}
    urls = params.get("urls", [])

    if not urls:
        links = await ProductLink.filter(task_id=task.id)
        urls = [l.url for l in links]

    if not urls:
        await task.fail("No URLs provided")
        return None

    if not ctx.context_id:
        await task.fail("Context has no context_id")
        return None

    from sanic import Sanic
    app = Sanic.get_app()
    playwright = app.ctx.playwright
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    logger.info(f"[product_detail_fetch] Using context_key={ctx.context_id}")
    context_result = await agent_bay.context.get(ctx.context_id, create=False)
    if not context_result.success:
        await task.fail(f"Context not found: {ctx.context_id}")
        return None

    await task.log_step(1, "创建浏览器会话", {"context_id": ctx.context_id}, {}, "running")

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "task", "task_type": "product_detail_fetch", "task_id": str(task.id)},
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

        await session.browser.initialize(
            BrowserOption(
                screen=BrowserScreen(width=1920, height=1080),
                solve_captchas=True,
                use_stealth=True,
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )

        endpoint = await session.browser.get_endpoint_url()
        browser = await playwright.chromium.connect_over_cdp(endpoint)
        bc = browser.contexts[0] if browser.contexts else await browser.new_context()
        bc.on("dialog", lambda d: d.dismiss())

        agent = session.browser.agent
        results = []
        total = len(urls)

        await task.log_step(1, "创建浏览器会话", {}, {"status": "ok"}, "completed")

        for i, url in enumerate(urls):
            if task.status == TaskStatus.CANCELLED.value:
                break

            try:
                await agent.navigate(url)
                await asyncio.sleep(5)

                await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")
                await asyncio.sleep(1)

                # 刷新 page 引用
                page = _get_page(bc)
                if not page:
                    results.append({"url": url, "error": "no active page"})
                    continue

                # 检测平台
                platform = await page.evaluate(_DETECT_PLATFORM_JS)
                detail_js = _PLATFORM_DETAIL_JS.get(platform, _GENERIC_DETAIL_JS)

                # 第一遍提取
                info = await page.evaluate(detail_js)

                # 滚动到页面底部加载详情图
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

                # 再提取一次详情图（滚动后可能加载更多）
                page = _get_page(bc)
                if page:
                    detail_imgs = await page.evaluate("""
                    () => {
                        const imgs = [];
                        document.querySelectorAll('#description img, [class*="desc"] img, #detail img, #J-detail-content img').forEach(img => {
                            const src = img.getAttribute('data-lazyload') || img.getAttribute('data-src') || img.getAttribute('src') || '';
                            if (src && src.startsWith('http')) imgs.push(src);
                        });
                        return imgs;
                    }
                    """)
                    if detail_imgs:
                        info["detail_images"] = list(set(info.get("detail_images", []) + detail_imgs))

                info["platform"] = platform
                info["url"] = url

                # 更新 ProductLink
                updated = await ProductLink.filter(url=url).update(
                    title=info.get("title", ""),
                    price=info.get("price", ""),
                    original_price=info.get("original_price", ""),
                    sales=info.get("sales", ""),
                    shop_name=info.get("shop_name", ""),
                    shop_url=info.get("shop_url", ""),
                    image=info.get("image", ""),
                    main_images=info.get("main_images", []),
                    location=info.get("location", ""),
                    sku_info=info.get("sku_info", []),
                    detail_images=info.get("detail_images", []),
                    platform=platform,
                )

                # 如果 ProductLink 不存在则创建
                if updated == 0:
                    await ProductLink.create(
                        task_id=task.id,
                        url=url,
                        platform=platform,
                        title=info.get("title", ""),
                        price=info.get("price", ""),
                        original_price=info.get("original_price", ""),
                        sales=info.get("sales", ""),
                        shop_name=info.get("shop_name", ""),
                        shop_url=info.get("shop_url", ""),
                        image=info.get("image", ""),
                        main_images=info.get("main_images", []),
                        location=info.get("location", ""),
                        sku_info=info.get("sku_info", []),
                        detail_images=info.get("detail_images", []),
                    )

                results.append({"url": url, "platform": platform, "title": info.get("title", "")[:40], "updated": updated > 0})
                logger.info(f"[product_detail_fetch] {i+1}/{total} {platform} title={info.get('title', '')[:40]} url={url[:60]}")

                task.progress = int((i + 1) / total * 100)
                await task.save()

            except Exception as e:
                logger.warning(f"[product_detail_fetch] failed {url[:60]}: {e}")
                results.append({"url": url, "error": str(e)})

        await task.log_step(2, f"商品详情抓取完成: {len(results)} 条", {}, {"fetched": len(results)}, "completed")

        return {
            "total": len(results),
            "success": sum(1 for r in results if not r.get("error")),
            "failed": sum(1 for r in results if r.get("error")),
            "results": results,
        }

    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        await agent_bay.delete(session, sync_context=False)


def _get_page(browser_context):
    for p in browser_context.pages:
        if not p.is_closed():
            return p
    return None


# ── Standalone 模式 ──

async def _run_standalone() -> None:
    parser = argparse.ArgumentParser(description="Standalone product detail fetch")
    parser.add_argument("--urls", required=True, help="逗号分隔的商品链接")
    parser.add_argument("--context-key", required=True, help="浏览器上下文 key")
    parser.add_argument("--output", default="/tmp/product_details.json")
    args = parser.parse_args()

    urls = [u.strip() for u in args.urls.split(",") if u.strip()]
    if not urls:
        print("No URLs provided")
        return

    from playwright.async_api import async_playwright

    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)
    context_result = await agent_bay.context.get(args.context_key, create=False)
    if not context_result.success:
        raise RuntimeError(f"Context not found: {args.context_key}")

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "standalone", "script": "product_detail_fetch"},
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
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )

        endpoint_url = await session.browser.get_endpoint_url()
        playwright = await async_playwright().start()
        browser = await playwright.chromium.connect_over_cdp(endpoint_url)
        bc = browser.contexts[0] if browser.contexts else await browser.new_context()
        bc.on("dialog", lambda d: d.dismiss())

        agent = session.browser.agent
        results = []

        for i, url in enumerate(urls):
            try:
                await agent.navigate(url)
                await asyncio.sleep(5)
                await _agent_act(agent, "关闭页面上所有弹框、登录提示、广告弹窗。")
                await asyncio.sleep(1)

                page = _get_page(bc)
                if not page:
                    results.append({"url": url, "error": "no active page"})
                    continue

                platform = await page.evaluate(_DETECT_PLATFORM_JS)
                detail_js = _PLATFORM_DETAIL_JS.get(platform, _GENERIC_DETAIL_JS)
                info = await page.evaluate(detail_js)
                info["platform"] = platform
                info["url"] = url
                results.append(info)
                print(f"[{i+1}/{len(urls)}] {platform} {info.get('title', '')[:40]}")

            except Exception as e:
                print(f"[{i+1}/{len(urls)}] FAILED {url[:60]}: {e}")
                results.append({"url": url, "error": str(e)})

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(results)} items to {args.output}")

    finally:
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        if playwright:
            await playwright.stop()
        await agent_bay.delete(session, sync_context=False)


if __name__ == "__main__":
    asyncio.run(_run_standalone())
