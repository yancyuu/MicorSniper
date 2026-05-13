# -*- coding: utf-8 -*-
"""商品详情抓取：打开商品链接，提取完整商品信息，更新到 ProductLink。

用法:
  poetry run python -m scripts.product_detail_fetch --urls "url1,url2" --context-key "xxx"
"""

import argparse
import asyncio
import random
import json
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
from models.product_detail import ProductDetail
from models.product_link import ProductLink, ProductLinkMonitorStatus
from models.intel_link import IntelLink
from models.task import Task, TaskStatus
from services.product_detail import get_provider_service
from utils.login_check import check_login_status, login_failed_message
from utils.logger import logger


async def _dismiss_dialog(dialog) -> None:
    try:
        await dialog.dismiss()
    except Exception as e:
        logger.debug(f"[product_detail_fetch] dialog already closed: {e}")


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


async def _close_popups_fast(page) -> None:
    """用短平快 DOM 操作关闭常见弹层，避免 agent.act 长时间等待。"""
    try:
        await page.evaluate(
            """
            () => {
                const selectors = [
                    '.J-close', '.close', '.btn-close', '.dialog-close', '.modal-close',
                    '[class*="close"]', '[aria-label="关闭"]', '[title="关闭"]'
                ];
                for (const selector of selectors) {
                    document.querySelectorAll(selector).forEach((el) => {
                        try { el.click(); } catch (e) {}
                    });
                }
            }
            """
        )
    except Exception as e:
        logger.debug(f"[product_detail_fetch] fast popup close ignored: {e}")


async def _settle_product_page(page, platform: str) -> None:
    delay = 2.5 if platform == "jd" else 0.8
    final_delay = 3.0 if platform == "jd" else 1.0
    for y in [300, 900, 1500, 2300, 3200]:
        await page.evaluate("(y) => window.scrollTo(0, y)", y)
        await asyncio.sleep(delay)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(final_delay)


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


async def _claim_detail_links(params: dict, task: Task, batch_size: int, link_model=ProductLink) -> list:
    platforms = params.get("platforms") or params.get("source_platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    platform_sql = "AND platform = ANY($3::text[])" if platforms else ""
    args = [str(task.id), batch_size]
    if platforms:
        args.append(platforms)
    table = link_model._meta.db_table
    conn = Tortoise.get_connection("default")
    await conn.execute_query(
        f"""
        UPDATE {table}
        SET lock_task_id = $1::uuid, locked_at = NOW()
        WHERE id IN (
            SELECT id FROM {table}
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
    links = await link_model.filter(lock_task_id=task.id).order_by("created_at")
    shards = params.get("shards") or {}
    if shards:
        filtered = []
        for link in links:
            shard = shards.get(link.platform)
            if shard and _stable_shard(link.url, int(shard.get("count") or 1)) == int(shard.get("index") or 0):
                filtered.append(link)
            else:
                await link_model.filter(id=link.id).update(lock_task_id=None, locked_at=None)
        links = filtered
    return links


async def _load_detail_urls(params: dict, task: Task, link_model=ProductLink) -> list[str]:
    platforms = params.get("platforms") or params.get("source_platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    retry_missing = bool(params.get("retry_missing"))

    if params.get("source_task_id") or params.get("task_id"):
        query = link_model.filter(task_id=params.get("source_task_id") or params.get("task_id"))
    else:
        query = link_model.filter(monitor_status=ProductLinkMonitorStatus.MONITORED.value)
    if platforms:
        query = query.filter(platform__in=platforms)

    links = await query.order_by("created_at")
    shards = params.get("shards") or {}
    if shards:
        filtered = []
        for link in links:
            shard = shards.get(link.platform)
            if not shard:
                continue
            if _stable_shard(link.url, int(shard.get("count") or 1)) == int(shard.get("index") or 0):
                filtered.append(link)
        links = filtered
    if not links:
        return []
    if not retry_missing:
        return [link.url for link in links]

    details = await ProductDetail.filter(url__in=[link.url for link in links])
    detail_map = {detail.url: detail for detail in details}
    retry_urls = []
    for link in links:
        detail = detail_map.get(link.url)
        missing_summary = not (link.title and link.price and link.image and link.shop_name)
        missing_detail = (
            detail is None
            or not detail.title
            or not detail.price
            or not detail.image
            or not detail.shop_name
            or not detail.sku_info
            or not detail.detail_images
        )
        if missing_summary or missing_detail:
            retry_urls.append(link.url)
    return retry_urls


async def run_product_detail_fetch(task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
    """打开商品链接，提取完整商品信息，更新到 ProductLink / IntelLink。"""
    params = task.params or {}
    is_intel = params.get("source") == "intel"
    LinkModel = IntelLink if is_intel else ProductLink
    detail_source = "intel" if is_intel else "product"
    urls = [LinkModel.canonicalize_url(url) for url in (params.get("urls", []) or [])]

    if not urls:
        batch_size = int(params.get("batch_size") or 100)
        if params.get("retry_missing") or params.get("source_task_id") or params.get("task_id"):
            urls = await _load_detail_urls(params, task, link_model=LinkModel)
        else:
            links = await _claim_detail_links(params, task, batch_size, link_model=LinkModel)
            urls = [link.url for link in links]

    if not urls:
        await task.fail("No URLs provided")
        return None

    all_urls = list(urls)
    max_batch_size = int(params.get("batch_size") or len(all_urls))
    batch_size = random.randint(5, min(8, max_batch_size)) if max_batch_size > 5 else max_batch_size

    # 平台差异化批次：淘宝/天猫更激进，JD 保持保守
    primary_platforms = set()
    for url in urls:
        host = urlparse(url).hostname or ""
        if "jd.com" in host:
            primary_platforms.add("jd")
        elif "taobao.com" in host or "tmall.com" in host or "tmall.hk" in host:
            primary_platforms.add("taobao")
    if "jd" not in primary_platforms and max_batch_size > 8:
        batch_size = random.randint(8, min(12, max_batch_size))
    current_offset = int(params.get("current_offset") or 0)
    batch_index = int(params.get("batch_index") or 1)
    interval_minutes = int(params.get("batch_interval_minutes") or 0)
    interval_minutes = interval_minutes + int(random.uniform(0, 10)) if interval_minutes else 0
    urls = all_urls[current_offset : current_offset + batch_size]
    if not urls:
        await task.complete({"total": 0, "success": 0, "failed": 0, "message": "No remaining URLs"})
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

    logger.info(f"[product_detail_fetch] Using context_key={ctx.context_id}")
    context_result = await agent_bay.context.get(ctx.context_id, create=False)
    if not context_result.success:
        await task.fail(f"Context not found: {ctx.context_id}")
        return None

    base_step = len(task.logs or [])
    await task.log_step(base_step + 1, "创建浏览器会话", {"context_id": ctx.context_id}, {}, "running")

    session_result = await agent_bay.create(
        CreateSessionParams(
            labels={"app": "micro-sniper", "kind": "task", "task_type": "product_detail_fetch", "task_id": str(task.id)},
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
        browser = await asyncio.wait_for(playwright.chromium.connect_over_cdp(endpoint), timeout=30)
        bc = browser.contexts[0] if browser.contexts else await browser.new_context()
        bc.on("dialog", lambda d: asyncio.create_task(_dismiss_dialog(d)))

        agent = session.browser.agent
        results = []
        total = len(urls)

        await task.log_step(base_step + 1, "创建浏览器会话", {}, {"status": "ok"}, "completed")
        await task.log_step(
            base_step + 2,
            f"执行第 {batch_index} 批商品详情抓取",
            {"offset": current_offset, "batch_size": batch_size, "total_urls": len(all_urls)},
            {},
            "running",
        )

        # FR-1: 每批次执行前校验登录态
        # 检测本批次 URL 的主要平台
        platforms_in_batch = []
        for url in urls:
            host = urlparse(url).hostname or ""
            if "jd.com" in host:
                platforms_in_batch.append("jd")
            elif "tmall.com" in host or "tmall.hk" in host:
                platforms_in_batch.append("tmall")
            elif "taobao.com" in host:
                platforms_in_batch.append("taobao")
            elif "1688.com" in host:
                platforms_in_batch.append("1688")
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
                await task.log_step(step, f"抓取商品详情 {i + 1}/{total}", {"url": url}, {}, "running")
                await _prepare_fresh_page(bc)
                page = _get_page(bc)
                if not page:
                    error = "no active page"
                    results.append({"url": url, "error": error})
                    await task.log_step(step, f"抓取商品详情失败 {i + 1}/{total}", {"url": url}, {"error": error}, "failed")
                    continue
                await asyncio.wait_for(page.goto(url, wait_until="domcontentloaded", timeout=45000), timeout=50)
                await asyncio.sleep(3)
                await _close_popups_fast(page)
                await asyncio.sleep(1)

                page = _get_page(bc)
                if not page:
                    error = "no active page"
                    results.append({"url": url, "error": error})
                    await task.log_step(step, f"抓取商品详情失败 {i + 1}/{total}", {"url": url}, {"error": error}, "failed")
                    continue

                platform = await page.evaluate(_DETECT_PLATFORM_JS)
                await _settle_product_page(page, platform)
                await task.log_step(step, f"提取{platform}商品详情 {i + 1}/{total}", {"url": url, "platform": platform}, {}, "running")
                provider_service = get_provider_service(platform)
                info = await provider_service.extract(page)

                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

                page = _get_page(bc)
                if page:
                    detail_imgs = await provider_service.extract_detail_images(page)
                    if detail_imgs:
                        info["detail_images"] = list(set(info.get("detail_images", []) + detail_imgs))

                # FR-4: 检测商品是否失效（仅在登录态正常后执行）
                page_text = await page.evaluate("() => document.body ? document.body.innerText : ''")
                invalid_reason = _check_product_invalid(platform, page_text, info.get("title", ""), info.get("price", ""))

                info["platform"] = platform
                url = LinkModel.canonicalize_url(url)
                info["url"] = url

                update_data = {"task_id": task.id, "platform": platform}
                for field in ["title", "price", "sales", "shop_name", "image"]:
                    value = info.get(field)
                    if value:
                        update_data[field] = value

                # 如果确认商品失效，标记状态
                if invalid_reason:
                    update_data["monitor_status"] = ProductLinkMonitorStatus.INVALID.value
                    logger.info(f"[product_detail_fetch] 商品失效: {url[:60]} - {invalid_reason}")

                updated = await LinkModel.filter(url=url).update(**update_data)
                link = await LinkModel.filter(url=url).first()

                if updated == 0:
                    if invalid_reason:
                        update_data["monitor_status"] = ProductLinkMonitorStatus.INVALID.value
                    link = await LinkModel.create(
                        task_id=task.id,
                        url=url,
                        raw_url=url,
                        platform=platform,
                        title=info.get("title", ""),
                        price=info.get("price", ""),
                        sales=info.get("sales", ""),
                        shop_name=info.get("shop_name", ""),
                        image=info.get("image", ""),
                        monitor_status=update_data.get("monitor_status", ProductLinkMonitorStatus.MONITORED.value),
                    )

                await ProductDetail.upsert_from_info(
                    task_id=task.id,
                    product_link_id=link.id if link else None,
                    platform=platform,
                    url=url,
                    info=info,
                    source=detail_source,
                )

                results.append({"url": url, "platform": platform, "title": info.get("title", "")[:40], "updated": updated > 0})
                logger.info(f"[product_detail_fetch] {i+1}/{total} {platform} title={info.get('title', '')[:40]} url={url[:60]}")
                await task.log_step(
                    step,
                    f"完成{platform}商品详情 {i + 1}/{total}",
                    {"url": url, "platform": platform},
                    {
                        "title": info.get("title", "")[:80],
                        "price": info.get("price", ""),
                        "shop_name": info.get("shop_name", ""),
                        "detail_images": len(info.get("detail_images", [])),
                    },
                    "completed",
                )

                task.progress = int((i + 1) / total * 100)
                await task.save()

            except Exception as e:
                logger.warning(f"[product_detail_fetch] failed {url[:60]}: {e}")
                results.append({"url": url, "error": str(e)})
                logger.info(f"[product_detail_fetch] skipped status change for {url[:60]}: transient error")
                await task.log_step(step, f"抓取商品详情失败 {i + 1}/{total}", {"url": url}, {"error": str(e)}, "failed")
            finally:
                params["current_offset"] = current_offset + i + 1
                task.params = params
                try:
                    await _reset_to_idle_page(bc, f"已完成 {i + 1}/{total}，等待下一条链接")
                except Exception as e:
                    logger.debug(f"[product_detail_fetch] reset idle page ignored: {e}")

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
                base_step + total + 4,
                f"第 {batch_index} 批完成，等待下次执行",
                {},
                {"fetched": len(results), "next_offset": next_offset, "remaining": len(all_urls) - next_offset},
                "completed",
            )
            await task.save()
            await LinkModel.filter(lock_task_id=task.id).update(lock_task_id=None, locked_at=None)
            return {
                "total": next_offset,
                "success": sum(1 for r in results if not r.get("error")),
                "failed": sum(1 for r in results if r.get("error")),
                "results": results,
                "queued_next": True,
            }

        params["current_offset"] = current_offset + len(results)
        task.params = params
        task.not_before_at = None
        await LinkModel.filter(lock_task_id=task.id).update(lock_task_id=None, locked_at=None)
        await task.save()
        await task.log_step(base_step + total + 4, f"商品详情抓取完成: {current_offset + len(results)} 条", {}, {"fetched": len(results)}, "completed")

        return {
            "total": current_offset + len(results),
            "success": sum(1 for r in results if not r.get("error")),
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
            logger.warning(f"[product_detail_fetch] Failed to close browser (continuing): {e!r}")

        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
        except Exception as e:
            logger.warning(f"[product_detail_fetch] cleanup session failed: {e!r}")


def _get_page(browser_context):
    for p in browser_context.pages:
        if not p.is_closed():
            return p
    return None


async def _prepare_fresh_page(browser_context) -> None:
    """每个 URL 使用一个干净页面，避免长任务中标签页和 DOM 资源堆积。"""
    page = _get_page(browser_context)
    if not page:
        page = await browser_context.new_page()
    await _close_open_pages(browser_context, keep=page)
    await page.goto("about:blank")
    await page.bring_to_front()


async def _close_open_pages(browser_context, keep=None) -> None:
    for page in list(browser_context.pages):
        try:
            if keep is not None and page == keep:
                continue
            if not page.is_closed():
                await page.close()
        except Exception as e:
            logger.debug(f"[product_detail_fetch] close page ignored: {e}")


async def _reset_to_idle_page(browser_context, message: str = "等待下一条链接") -> None:
    """关闭重页面后保留一个轻量状态页，避免云浏览器 iframe 白屏。"""
    page = _get_page(browser_context)
    if not page:
        page = await browser_context.new_page()
    await _close_open_pages(browser_context, keep=page)
    await page.set_content(
        f"""
        <html>
          <body style="margin:0;font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f6f8fc;color:#344054;display:grid;place-items:center;height:100vh;">
            <div style="text-align:center">
              <div style="font-size:14px;font-weight:700;color:#101828;margin-bottom:6px">Micro Sniper 正在处理</div>
              <div style="font-size:12px">{message}</div>
            </div>
          </body>
        </html>
        """
    )
    await page.bring_to_front()


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
                fingerprint=BrowserFingerprint(devices=["desktop"], operating_systems=["windows"], locales=["zh-CN"]),
            )
        )

        endpoint_url = await session.browser.get_endpoint_url()
        playwright = await async_playwright().start()
        browser = await asyncio.wait_for(playwright.chromium.connect_over_cdp(endpoint_url), timeout=30)
        bc = browser.contexts[0] if browser.contexts else await browser.new_context()
        bc.on("dialog", lambda d: asyncio.create_task(_dismiss_dialog(d)))

        agent = session.browser.agent
        results = []

        for i, url in enumerate(urls):
            try:
                await _prepare_fresh_page(bc)
                page = _get_page(bc)
                if not page:
                    results.append({"url": url, "error": "no active page"})
                    continue
                await asyncio.wait_for(page.goto(url, wait_until="domcontentloaded", timeout=45000), timeout=50)
                await asyncio.sleep(3)
                await _close_popups_fast(page)
                await asyncio.sleep(1)

                page = _get_page(bc)
                if not page:
                    results.append({"url": url, "error": "no active page"})
                    continue

                platform = await page.evaluate(_DETECT_PLATFORM_JS)
                provider_service = get_provider_service(platform)
                info = await provider_service.extract(page)

                await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                await asyncio.sleep(2)

                page = _get_page(bc)
                if page:
                    detail_imgs = await provider_service.extract_detail_images(page)
                    if detail_imgs:
                        info["detail_images"] = list(set(info.get("detail_images", []) + detail_imgs))

                info["platform"] = platform
                info["url"] = url
                results.append(info)
                print(f"[{i+1}/{len(urls)}] {platform} {info.get('title', '')[:40]}")

            except Exception as e:
                print(f"[{i+1}/{len(urls)}] FAILED {url[:60]}: {e}")
                results.append({"url": url, "error": str(e)})
            finally:
                try:
                    await _reset_to_idle_page(bc, f"已完成 {i + 1}/{len(urls)}，等待下一条链接")
                except Exception as e:
                    logger.debug(f"[product_detail_fetch] reset idle page ignored: {e}")

        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print(f"\nSaved {len(results)} items to {args.output}")

    finally:
        async def _close_browser():
            if browser:
                await browser.close()

        try:
            await asyncio.wait_for(_close_browser(), timeout=10)
        except Exception as e:
            logger.warning(f"[product_detail_fetch] Failed to close browser (continuing): {e!r}")
        if playwright:
            await playwright.stop()
        try:
            await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
        except Exception as e:
            logger.warning(f"[product_detail_fetch] cleanup session failed: {e!r}")


if __name__ == "__main__":
    asyncio.run(_run_standalone())
