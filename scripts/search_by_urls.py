# -*- coding: utf-8 -*-
"""按 URL 批量打开商品页，提取当前价格并更新 ProductLink。

用法:
  poetry run python -m scripts.search_by_urls --urls "url1,url2" --context-key "xxx"
"""

import asyncio
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse
from tortoise import Tortoise

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
from utils.login_check import check_login_status, login_failed_message
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
        function cleanText(text) {
            return String(text || '').replace(/\\s+/g, ' ').trim();
        }
        function normalizePrice(text) {
            const value = String(text || '');
            if (/(已售|销量|月销|评价|评论|人购买|人收货)/.test(value)) return '';
            const marked = value.match(/[¥￥]\\s*([\\d,.]+)/);
            if (marked) return marked[1].replace(/,/g, '');
            const m = value.match(/\\d+(?:\\.\\d+)?/);
            return m ? m[0] : '';
        }
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return cleanText(el.textContent);
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
        const priceText = pickText(['#jd-price', '.summary-price .p-price .price', '.p-price .price', '[class*="J-p-"]']);
        const price = normalizePrice(priceText) || normalizePrice((body.match(/(?:京东价|到手价|秒杀价|券后价)?\\s*[¥￥]\\s*[\\d,.]+/) || [])[0]);
        return {
            price,
            title: pickText(['.sku-name', 'h1']) || document.title,
            shop_name: pickText(['.J-hove-wrap .name', '[class*="shopName"]', '[class*="storeName"]']),
            sales: pickText(['#comment-count a', '[class*="comment"]']),
            image: pickImage(),
        };
    }
    """,
    "taobao": """
    () => {
        function cleanText(text) {
            return String(text || '').replace(/\\s+/g, ' ').trim();
        }
        function normalizePrice(text) {
            const value = String(text || '');
            if (/(已售|销量|月销|评价|评论|人付款|人收货)/.test(value)) return '';
            const marked = value.match(/[¥￥]\\s*([\\d,.]+)/);
            if (marked) return marked[1].replace(/,/g, '');
            const m = value.match(/\\d+(?:\\.\\d+)?/);
            return m ? m[0] : '';
        }
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return cleanText(el.textContent);
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
        const priceText = pickText(['[class*="priceText"], [class*="Price--priceText"], [class*="priceText--"], [class*="price"], [class*="Price"]']);
        const price = normalizePrice(priceText) || normalizePrice((body.match(/(?:券后|到手价)?\\s*[¥￥]\\s*[\\d,.]+/) || [])[0]);
        return {
            price,
            title: pickText(['h1', '[class*="title"], [class*="Title"]']) || document.title,
            shop_name: pickText(['[class*="shop"], [class*="Shop"], [class*="store"], [class*="Store"]']),
            sales: (body.match(/(月销|已售|销量)\\s*([\\d,.万+]+)/) || [])[0] || '',
            image: pickImage(),
        };
    }
    """,
    "tmall": """
    () => {
        function cleanText(text) {
            return String(text || '').replace(/\\s+/g, ' ').trim();
        }
        function normalizePrice(text) {
            const value = String(text || '');
            if (/(已售|销量|月销|评价|评论|人付款|人收货)/.test(value)) return '';
            const marked = value.match(/[¥￥]\\s*([\\d,.]+)/);
            if (marked) return marked[1].replace(/,/g, '');
            const m = value.match(/\\d+(?:\\.\\d+)?/);
            return m ? m[0] : '';
        }
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return cleanText(el.textContent);
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
        const priceText = pickText(['[class*="priceText"], [class*="Price--priceText"], '.tm-price', '[class*="price"], [class*="Price"]']);
        const price = normalizePrice(priceText) || normalizePrice((body.match(/(?:券后|到手价)?\\s*[¥￥]\\s*[\\d,.]+/) || [])[0]);
        return {
            price,
            title: pickText(['h1', '[class*="title"], [class*="Title"]']) || document.title,
            shop_name: pickText(['[class*="shop"], [class*="Shop"], [class*="store"], [class*="Store"]']),
            sales: (body.match(/(月销|已售|销量)\\s*([\\d,.万+]+)/) || [])[0] || '',
            image: pickImage(),
        };
    }
    """,
    "1688": """
    () => {
        function cleanText(text) {
            return String(text || '').replace(/\\s+/g, ' ').trim();
        }
        function normalizePrice(text) {
            const value = String(text || '');
            if (/(成交|已售|销量|月销|评价|评论)/.test(value)) return '';
            const marked = value.match(/[¥￥]\\s*([\\d,.]+)/);
            if (marked) return marked[1].replace(/,/g, '');
            const m = value.match(/\\d+(?:\\.\\d+)?/);
            return m ? m[0] : '';
        }
        function pickText(selectors) {
            for (const selector of selectors) {
                const el = document.querySelector(selector);
                if (el && el.textContent.trim()) return cleanText(el.textContent);
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
        const price = normalizePrice(priceText) || normalizePrice((body.match(/[¥￥]\\s*[\\d,.]+/) || [])[0]);
        return {
            price,
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


def _stable_shard(url: str, count: int) -> int:
    return sum(ord(ch) for ch in url) % max(count, 1)


def _check_product_invalid(platform: str, page_text: str, title: str, price: str) -> str | None:
    """检测商品是否失效，返回失效原因或 None

    Args:
        platform: 平台名称
        page_text: 页面文本内容
        title: 提取的商品标题
        price: 提取的价格

    Returns:
        失效原因字符串，或 None（商品有效或不确定）
    """
    if not page_text:
        return None

    text = page_text.lower()

    # 各平台失效标志
    invalid_patterns = {
        "jd": ["商品已下架", "该商品已售罄", "商品不存在", "很抱歉，您查看的商品已下架"],
        "taobao": ["此宝贝已下架", "商品已下架", "宝贝不存在", "该商品已失效"],
        "tmall": ["此宝贝已下架", "商品已下架", "宝贝不存在", "该商品已失效"],
        "1688": ["商品已下架", "商品已失效", "商品不存在", "已下架"],
    }

    patterns = invalid_patterns.get(platform, [])
    for pattern in patterns:
        if pattern in page_text:
            return f"商品失效: {pattern}"

    # 如果标题和价格都为空，且页面没有正常商品信息，可能是失效
    if not title and not price:
        # 检查是否是登录页面（如果包含"登录"、"注册"等，可能是登录拦截而非商品失效）
        login_keywords = ["登录", "注册", "login", "sign in"]
        if not any(kw in text for kw in login_keywords):
            return "商品信息缺失（可能已下架或不存在）"

    return None


async def _claim_links(params: dict, task: Task, batch_size: int) -> list[ProductLink]:
    platforms = params.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    shards = params.get("shards") or {}
    platform_sql = "AND platform = ANY($3::text[])" if platforms else ""
    args = [str(task.id), batch_size]
    if platforms:
        args.append(platforms)
    conn = Tortoise.get_connection("default")
    await conn.execute_query(
        f"""
        UPDATE product_links
        SET lock_task_id = $1::uuid, locked_at = NOW()
        WHERE id IN (
            SELECT id FROM product_links
            WHERE monitor_status = 'monitored'
              AND lock_task_id IS NULL
              {platform_sql}
            ORDER BY created_at
            LIMIT $2
            FOR UPDATE SKIP LOCKED
        )
        """,
        args,
    )
    links = await ProductLink.filter(lock_task_id=task.id).order_by("created_at")
    if shards:
        filtered = []
        for link in links:
            shard = shards.get(link.platform)
            if shard and _stable_shard(link.url, int(shard.get("count") or 1)) == int(shard.get("index") or 0):
                filtered.append(link)
            else:
                await ProductLink.filter(id=link.id).update(lock_task_id=None, locked_at=None)
        links = filtered
    return links


async def run_search_by_urls(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    """按 URL 批量打开商品链接并提取价格。"""
    params = task.params or {}
    urls = [ProductLink.canonicalize_url(url) for url in _normalize_urls(params.get("urls", []))]

    if not urls:
        batch_size = int(params.get("batch_size") or 100)
        links = await _claim_links(params, task, batch_size)
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
            image_id=global_settings.agentbay.image_id,
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
        base_step = len(task.logs or [])
        await task.log_step(base_step + 1, "创建浏览器会话", {}, {"status": "ok"}, "completed")
        await task.log_step(
            base_step + 2,
            f"执行第 {batch_index} 批轻量价格监控",
            {"offset": current_offset, "batch_size": batch_size, "total_urls": len(all_urls)},
            {},
            "running",
        )

        # FR-1: 每批次执行前校验登录态
        platforms_in_batch = list({url: _detect_channel_from_url(url) for url in urls}.values())
        primary_platform = platforms_in_batch[0] if platforms_in_batch else "unknown"
        login_result = await check_login_status(agent, primary_platform)
        if login_result.logged_in:
            await task.log_step(
                base_step + 3,
                f"登录态校验通过（{primary_platform}）",
                {"platform": primary_platform},
                {"logged_in": True},
                "completed",
            )
        else:
            await task.log_step(
                base_step + 3,
                f"登录态校验失败（{primary_platform}）",
                {"platform": primary_platform},
                {"logged_in": False, "reason": login_result.reason},
                "failed",
            )
            await task.fail(login_failed_message(primary_platform))
            return None

        for i, url in enumerate(urls):
            if task.status == TaskStatus.CANCELLED.value:
                break

            step = base_step + i + 4
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
                title = product.get("title", "")

                # FR-4: 检测商品是否失效（仅在登录态正常后执行）
                # 检查页面内容是否包含失效标志
                page_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                invalid_reason = _check_product_invalid(platform, page_text, title, price)

                # 更新 ProductLink
                update_data = {"task_id": task.id, "platform": platform}
                for field in ["price", "title", "shop_name", "sales", "image"]:
                    value = product.get(field)
                    if value:
                        update_data[field] = str(value)[:500] if field == "title" else value
                url = ProductLink.canonicalize_url(url)

                # 如果确认商品失效，标记状态
                if invalid_reason:
                    update_data["monitor_status"] = ProductLinkMonitorStatus.INVALID.value
                    logger.info(f"[search_by_urls] 商品失效: {url[:60]} - {invalid_reason}")

                updated = await ProductLink.filter(url=url).update(**update_data)
                if not updated:
                    if invalid_reason:
                        update_data["monitor_status"] = ProductLinkMonitorStatus.INVALID.value
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
                        monitor_status=update_data.get("monitor_status", ProductLinkMonitorStatus.MONITORED.value),
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
                logger.info(f"[search_by_urls] skipped status change for {url[:60]}: transient error")
                await task.log_step(step, f"轻量检查失败 {i + 1}/{total}", {"url": url}, {"error": str(e)}, "failed")
            finally:
                params["current_offset"] = current_offset + i + 1
                task.params = params
                task.progress = int((i + 1) / total * 100)
                await task.save()

        next_offset = current_offset + len(urls)
        has_more = len(urls) == batch_size
        if has_more and interval_minutes > 0:
            params["current_offset"] = next_offset
            params["batch_index"] = batch_index + 1
            task.params = params
            task.not_before_at = datetime.now() + timedelta(minutes=interval_minutes)
            task.status = TaskStatus.PENDING.value
            task.progress = int(next_offset / len(all_urls) * 100)
            await task.log_step(
                base_step + total + 4,
                f"第 {batch_index} 批完成，等待下次执行",
                {},
                {"checked": len(results), "next_offset": next_offset, "remaining": len(all_urls) - next_offset},
                "completed",
            )
            await task.save()
            await ProductLink.filter(lock_task_id=task.id).update(lock_task_id=None, locked_at=None)
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
        await ProductLink.filter(lock_task_id=task.id).update(lock_task_id=None, locked_at=None)
        await task.save()
        await task.log_step(base_step + total + 4, f"轻量价格监控完成: {current_offset + len(results)} 条", {}, {"checked": len(results)}, "completed")

        return {
            "total": current_offset + len(results),
            "updated": sum(1 for r in results if r.get("updated")),
            "failed": sum(1 for r in results if r.get("error")),
            "results": results,
        }

    finally:
        async def _close_browser():
            if browser:
                await browser.close()

        try:
            await asyncio.wait_for(_close_browser(), timeout=10)
        except Exception as e:
            logger.warning(f"[search_by_urls] Failed to close browser (continuing): {e!r}")

        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
        except Exception as e:
            logger.warning(f"[search_by_urls] Failed to delete session: {e!r}")
