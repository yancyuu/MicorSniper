# -*- coding: utf-8 -*-
"""按 URL 批量打开商品页，提取当前价格并更新 ProductLink。

用法:
  poetry run python -m scripts.search_by_urls --urls "url1,url2" --context-key "xxx"
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

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
from models.product_link import ProductLink, ProductLinkMonitorStatus
from models.task import Task, TaskStatus
from utils.logger import logger


async def _agent_act(agent, instruction: str, retries: int = 2) -> bool:
    for attempt in range(retries):
        try:
            ret = await agent.act(ActOptions(action=instruction))
            return bool(getattr(ret, "success", False))
        except Exception as e:
            logger.warning(f"[search_by_urls] agent.act failed (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return False


_EXTRACT_FALLBACK_JS = """
() => {
    const body = document.body ? document.body.innerText : '';
    function textOf(selector) {
        const el = document.querySelector(selector);
        return el ? el.textContent.trim() : '';
    }
    function firstImage() {
        const img = document.querySelector('img[src], img[data-src]');
        if (!img) return '';
        const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
        return src.startsWith('//') ? 'https:' + src : src;
    }
    const priceMatch = body.match(/¥\\s*([\\d,.]+)/) || body.match(/([\\d,.]+)\\s*元/);
    return {
        price: priceMatch ? priceMatch[1] : '',
        title: textOf('h1') || document.title || '',
        shop_name: '',
        sales: '',
        image: firstImage(),
    };
}
"""

_DETECT_PLATFORM_JS = """
() => {
    const url = window.location.href;
    const host = window.location.hostname;
    if (host.includes('jd.com')) return 'jd';
    if (host.includes('taobao.com')) return 'taobao';
    if (host.includes('tmall.com')) return 'tmall';
    if (host.includes('1688.com')) return '1688';
    return 'unknown';
}
"""

# 平台专属商品信息提取
_PLATFORM_PRODUCT_JS = {
    "jd": """
    () => {
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return '';
        }
        function pickImage() {
            const img = document.querySelector('#spec-img, img[data-origin], img[src*="360buyimg.com"]');
            if (!img) return '';
            const src = img.getAttribute('data-origin') || img.getAttribute('data-src') || img.getAttribute('src') || '';
            return src.startsWith('//') ? 'https:' + src : src;
        }
        const body = document.body ? document.body.innerText : '';
        const priceText = pickText(['.p-price', '[class*="price"]', '#jd-price']);
        const priceMatch = priceText.match(/[\\d,.]+/) || body.match(/¥\\s*([\\d,.]+)/);
        return {
            price: priceMatch ? priceMatch[1] || priceMatch[0] : '',
            title: pickText(['.sku-name', 'h1']) || document.title,
            shop_name: pickText(['.J-hove-wrap .name', '[class*="shopName"]', '[class*="storeName"]']),
            sales: pickText(['#comment-count a', '[class*="comment"]']),
            image: pickImage(),
        };
    }
    """,
    "taobao": """
    () => {
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return '';
        }
        function pickImage() {
            const img = document.querySelector('img[class*="main"], img[src*="alicdn.com"], img[data-src*="alicdn.com"]');
            if (!img) return '';
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            return src.startsWith('//') ? 'https:' + src : src;
        }
        const body = document.body ? document.body.innerText : '';
        const priceText = pickText(['[class*="price"], [class*="Price"]']);
        const priceMatch = priceText.match(/[\\d,.]+/) || body.match(/¥\\s*([\\d,.]+)/);
        return {
            price: priceMatch ? priceMatch[1] || priceMatch[0] : '',
            title: pickText(['h1', '[class*="title"], [class*="Title"]']) || document.title,
            shop_name: pickText(['[class*="shop"], [class*="Shop"], [class*="store"], [class*="Store"]']),
            sales: (body.match(/(月销|已售|销量)\\s*([\\d,.万+]+)/) || [])[0] || '',
            image: pickImage(),
        };
    }
    """,
    "tmall": """
    () => {
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return '';
        }
        function pickImage() {
            const img = document.querySelector('img[class*="main"], img[src*="alicdn.com"], img[data-src*="alicdn.com"]');
            if (!img) return '';
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            return src.startsWith('//') ? 'https:' + src : src;
        }
        const body = document.body ? document.body.innerText : '';
        const priceText = pickText(['[class*="price"], [class*="Price"]', '.tm-price']);
        const priceMatch = priceText.match(/[\\d,.]+/) || body.match(/¥\\s*([\\d,.]+)/);
        return {
            price: priceMatch ? priceMatch[1] || priceMatch[0] : '',
            title: pickText(['h1', '[class*="title"], [class*="Title"]']) || document.title,
            shop_name: pickText(['[class*="shop"], [class*="Shop"], [class*="store"], [class*="Store"]']),
            sales: (body.match(/(月销|已售|销量)\\s*([\\d,.万+]+)/) || [])[0] || '',
            image: pickImage(),
        };
    }
    """,
    "1688": """
    () => {
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return el.textContent.trim();
            }
            return '';
        }
        function pickImage() {
            const img = document.querySelector('img[src*="alicdn.com"], img[data-src*="alicdn.com"], img[src]');
            if (!img) return '';
            const src = img.getAttribute('data-src') || img.getAttribute('src') || '';
            return src.startsWith('//') ? 'https:' + src : src;
        }
        const body = document.body ? document.body.innerText : '';
        const priceText = pickText(['[class*="price"], [class*="Price"]']);
        const priceMatch = priceText.match(/[\\d,.]+/) || body.match(/¥\\s*([\\d,.]+)/);
        return {
            price: priceMatch ? priceMatch[1] || priceMatch[0] : '',
            title: pickText(['h1', '[class*="title"], [class*="Title"]']) || document.title,
            shop_name: pickText(['[class*="company"], [class*="shop"], [class*="store"]']),
            sales: (body.match(/(成交|销量|已售)\\s*([\\d,.万+]+)/) || [])[0] || '',
            image: pickImage(),
        };
    }
    """,
}


def _normalize_urls(raw_urls: Any) -> list[str]:
    if isinstance(raw_urls, str):
        return [u.strip() for u in raw_urls.replace("\n", ",").split(",") if u.strip()]
    if isinstance(raw_urls, list):
        return [str(u).strip() for u in raw_urls if str(u).strip()]
    return []


def _detect_channel_from_url(url: str) -> str:
    host = urlparse(url).hostname or ""
    host = host.lower()
    if "jd.com" in host:
        return "jd"
    if "tmall.com" in host or "tmall.hk" in host:
        return "tmall"
    if "taobao.com" in host:
        return "taobao"
    if "1688.com" in host:
        return "1688"
    return "unknown"


async def run_search_by_urls(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    """按 URL 批量打开商品链接并提取价格。"""
    params = task.params or {}
    urls = _normalize_urls(params.get("urls", []))

    if not urls:
        source_task_id = params.get("source_task_id") or params.get("task_id") or task.id
        retry_missing = bool(params.get("retry_missing"))
        if params.get("source_task_id") or params.get("task_id"):
            query = ProductLink.filter(task_id=source_task_id)
        else:
            query = ProductLink.filter(monitor_status=ProductLinkMonitorStatus.MONITORED.value)
        platforms = params.get("platforms") or []
        if isinstance(platforms, str):
            platforms = [platforms]
        if platforms:
            query = query.filter(platform__in=platforms)
        if retry_missing:
            query = query.filter(price="")
        links = await query.order_by("created_at")
        urls = [l.url for l in links]

    if not urls:
        await task.fail("No URLs provided")
        return None

    all_urls = list(urls)
    batch_size = int(params.get("batch_size") or len(all_urls))
    current_offset = int(params.get("current_offset") or 0)
    batch_index = int(params.get("batch_index") or 1)
    interval_minutes = int(params.get("batch_interval_minutes") or 0)
    urls = all_urls[current_offset : current_offset + batch_size]
    if not urls:
        await task.complete({"total": 0, "updated": 0, "failed": 0, "message": "No remaining URLs"})
        return None
    task.not_before_at = None
    await task.save()

    if not ctx.context_id:
        await task.fail("Context has no context_id")
        return None

    from sanic import Sanic

    app = Sanic.get_app()
    playwright = app.ctx.playwright
    agent_bay = AsyncAgentBay(api_key=global_settings.agentbay.api_key)

    logger.info(f"[search_by_urls] Using context_key={ctx.context_id}")
    context_result = await agent_bay.context.get(ctx.context_id, create=False)
    if not context_result.success:
        await task.fail(f"Context not found: {ctx.context_id}")
        return None

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "task", "task_type": "search_by_urls", "task_id": str(task.id)},
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
        await task.log_step(
            2,
            f"执行第 {batch_index} 批轻量价格监控",
            {"offset": current_offset, "batch_size": batch_size, "total_urls": len(all_urls)},
            {},
            "running",
        )

        for i, url in enumerate(urls):
            if task.status == TaskStatus.CANCELLED.value:
                break

            step = i + 3
            try:
                await task.log_step(step, f"轻量检查商品价格 {i + 1}/{total}", {"url": url}, {}, "running")
                await agent.navigate(url)
                await asyncio.sleep(5)

                page = None
                for p in bc.pages:
                    if not p.is_closed():
                        page = p
                        break
                if not page:
                    await task.log_step(step, f"轻量检查失败 {i + 1}/{total}", {"url": url}, {"error": "no active page"}, "failed")
                    continue

                # 先按 URL 分流渠道；页面跳转后再用页面 host 兜底校正。
                platform = _detect_channel_from_url(url)
                if platform == "unknown":
                    platform = await page.evaluate(_DETECT_PLATFORM_JS)
                extract_js = _PLATFORM_PRODUCT_JS.get(platform, _EXTRACT_FALLBACK_JS)
                product = await page.evaluate(extract_js)
                if not isinstance(product, dict):
                    product = {}
                price = product.get("price", "")

                # 更新 ProductLink
                update_data = {"task_id": task.id, "platform": platform}
                for field in ["price", "title", "shop_name", "sales", "image"]:
                    value = product.get(field)
                    if value:
                        update_data[field] = str(value)[:500] if field == "title" else value
                updated = await ProductLink.filter(url=url).update(**update_data)
                if not updated:
                    await ProductLink.create(
                        task_id=task.id,
                        platform=platform,
                        url=url,
                        raw_url=url,
                        title=update_data.get("title", ""),
                        price=update_data.get("price", ""),
                        sales=update_data.get("sales", ""),
                        shop_name=update_data.get("shop_name", ""),
                        image=update_data.get("image", ""),
                    )
                    updated = 1

                results.append({"url": url, "platform": platform, "updated": updated > 0, **product})
                logger.info(f"[search_by_urls] {i + 1}/{len(urls)} {platform} price={price} url={url[:60]}")
                await task.log_step(
                    step,
                    f"完成轻量价格检查 {i + 1}/{total}",
                    {"url": url, "platform": platform},
                    {
                        "title": product.get("title", "")[:80],
                        "price": product.get("price", ""),
                        "shop_name": product.get("shop_name", ""),
                    },
                    "completed",
                )
                task.progress = int((i + 1) / total * 100)
                await task.save()

            except Exception as e:
                logger.warning(f"[search_by_urls] failed {url[:60]}: {e}")
                results.append({"url": url, "price": "", "error": str(e)})
                await ProductLink.filter(url=url).update(monitor_status=ProductLinkMonitorStatus.INVALID.value)
                await task.log_step(step, f"轻量检查失败 {i + 1}/{total}", {"url": url}, {"error": str(e)}, "failed")
            finally:
                task.progress = int((i + 1) / total * 100)
                await task.save()

        next_offset = current_offset + len(urls)
        has_more = next_offset < len(all_urls)
        if has_more and interval_minutes > 0:
            params["current_offset"] = next_offset
            params["batch_index"] = batch_index + 1
            task.params = params
            task.not_before_at = datetime.now() + timedelta(minutes=interval_minutes)
            task.status = TaskStatus.PENDING.value
            task.progress = int(next_offset / len(all_urls) * 100)
            await task.log_step(
                total + 3,
                f"第 {batch_index} 批完成，等待下次执行",
                {},
                {"checked": len(results), "next_offset": next_offset, "remaining": len(all_urls) - next_offset},
                "completed",
            )
            await task.save()
            return {
                "total": next_offset,
                "updated": sum(1 for r in results if r.get("updated")),
                "failed": sum(1 for r in results if r.get("error")),
                "results": results,
                "queued_next": True,
            }

        params["current_offset"] = current_offset + len(results)
        task.params = params
        task.not_before_at = None
        await task.save()
        await task.log_step(total + 3, f"轻量价格监控完成: {current_offset + len(results)} 条", {}, {"checked": len(results)}, "completed")

        return {
            "total": current_offset + len(results),
            "updated": sum(1 for r in results if r.get("updated")),
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
