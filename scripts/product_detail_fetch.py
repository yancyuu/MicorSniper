# -*- coding: utf-8 -*-
"""商品详情抓取：打开商品链接，提取完整商品信息，更新到 ProductLink。

用法:
  poetry run python -m scripts.product_detail_fetch --urls "url1,url2" --context-key "xxx"
"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta
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
from models.product_detail import ProductDetail
from models.product_link import ProductLink, ProductLinkMonitorStatus
from models.task import Task, TaskStatus
from services.product_detail import get_provider_service
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
    delay = 1.2 if platform == "jd" else 0.8
    final_delay = 1.5 if platform == "jd" else 1.0
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


async def _load_detail_urls(params: dict, task: Task) -> list[str]:
    platforms = params.get("platforms") or params.get("source_platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    retry_missing = bool(params.get("retry_missing"))

    if params.get("source_task_id") or params.get("task_id"):
        query = ProductLink.filter(task_id=params.get("source_task_id") or params.get("task_id"))
    else:
        query = ProductLink.filter(monitor_status=ProductLinkMonitorStatus.MONITORED.value)
    if platforms:
        query = query.filter(platform__in=platforms)

    links = await query.order_by("created_at")
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
    """打开商品链接，提取完整商品信息，更新到 ProductLink。"""
    params = task.params or {}
    urls = params.get("urls", [])

    if not urls:
        urls = await _load_detail_urls(params, task)

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
        browser = await asyncio.wait_for(playwright.chromium.connect_over_cdp(endpoint), timeout=30)
        bc = browser.contexts[0] if browser.contexts else await browser.new_context()
        bc.on("dialog", lambda d: asyncio.create_task(_dismiss_dialog(d)))

        agent = session.browser.agent
        results = []
        total = len(urls)

        await task.log_step(1, "创建浏览器会话", {}, {"status": "ok"}, "completed")
        await task.log_step(
            2,
            f"执行第 {batch_index} 批商品详情抓取",
            {"offset": current_offset, "batch_size": batch_size, "total_urls": len(all_urls)},
            {},
            "running",
        )

        for i, url in enumerate(urls):
            if task.status == TaskStatus.CANCELLED.value:
                break

            step = i + 3
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

                info["platform"] = platform
                info["url"] = url

                update_data = {"task_id": task.id, "platform": platform}
                for field in ["title", "price", "sales", "shop_name", "image"]:
                    value = info.get(field)
                    if value:
                        update_data[field] = value
                updated = await ProductLink.filter(url=url).update(**update_data)
                link = await ProductLink.filter(url=url).first()

                if updated == 0:
                    link = await ProductLink.create(
                        task_id=task.id,
                        url=url,
                        raw_url=url,
                        platform=platform,
                        title=info.get("title", ""),
                        price=info.get("price", ""),
                        sales=info.get("sales", ""),
                        shop_name=info.get("shop_name", ""),
                        image=info.get("image", ""),
                    )

                await ProductDetail.upsert_from_info(
                    task_id=task.id,
                    product_link_id=link.id if link else None,
                    platform=platform,
                    url=url,
                    info=info,
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
                await ProductLink.filter(url=url).update(monitor_status=ProductLinkMonitorStatus.INVALID.value)
                await task.log_step(step, f"抓取商品详情失败 {i + 1}/{total}", {"url": url}, {"error": str(e)}, "failed")
            finally:
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
                total + 3,
                f"第 {batch_index} 批完成，等待下次执行",
                {},
                {"fetched": len(results), "next_offset": next_offset, "remaining": len(all_urls) - next_offset},
                "completed",
            )
            await task.save()
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
        await task.save()
        await task.log_step(total + 3, f"商品详情抓取完成: {current_offset + len(results)} 条", {}, {"fetched": len(results)}, "completed")

        return {
            "total": current_offset + len(results),
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
        try:
            await agent_bay.delete(session, sync_context=False)
        except Exception as e:
            logger.warning(f"[product_detail_fetch] cleanup session failed: {e}")


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
        try:
            if browser:
                await browser.close()
        except Exception:
            pass
        if playwright:
            await playwright.stop()
        try:
            await agent_bay.delete(session, sync_context=False)
        except Exception as e:
            logger.warning(f"[product_detail_fetch] cleanup session failed: {e}")


if __name__ == "__main__":
    asyncio.run(_run_standalone())
