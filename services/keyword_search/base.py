# -*- coding: utf-8 -*-
"""关键词搜索 service 基类。

子类只需实现平台差异化部分（JS 提取器、URL 规范化、搜索 URL、分页方式），
公共逻辑（session 创建、登录校验、关键词循环、分批断点、清理）全部在基类 run() 中。
"""

import asyncio
import json
import random
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import quote

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
from models.product_link import ProductLink, ProductLinkMonitorStatus, ProductLinkSourceType
from models.task import Task, TaskStatus
from utils.logger import logger
from utils.login_check import check_login_status, login_failed_message

_DEFAULT_LIMIT = 1000
_MAX_LIMIT = 1000
_DEFAULT_MAX_PAGES = 100


class KeywordSearchService:
    """关键词搜索基类。子类实现平台差异化方法，基类负责完整 run 流程。"""

    platform: str = ""

    def build_search_url(self, keyword: str) -> str:
        raise NotImplementedError

    def canonicalize_url(self, url: str) -> str:
        raise NotImplementedError

    async def get_page(self, browser_context, fallback_url: str):
        raise NotImplementedError

    async def wait_page_ready(self, page) -> None:
        raise NotImplementedError

    async def scroll_and_extract(self, agent, page, browser_context, fallback_url) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def go_next_page(self, agent, page) -> bool:
        raise NotImplementedError

    async def after_navigate(self, agent) -> None:
        """导航到搜索页后的平台专属操作（关闭弹窗、排序等）。"""
        pass

    async def run(self, task: Task, ctx: BrowserContext) -> dict[str, Any] | None:
        """完整的任务执行流程，包含分批续跑。"""
        params = task.params or {}
        keywords = _normalize_keywords(params.get("keywords"))
        raw_limit = params.get("limit")
        limit = min(int(raw_limit), _MAX_LIMIT) if raw_limit else 0
        max_pages = int(params.get("max_pages") or _DEFAULT_MAX_PAGES)
        title_required = params.get("title_required", "")
        title_blacklist = params.get("title_blacklist", [])
        from config.crawl_profile import get_profile
        _cp = get_profile(getattr(self, "platform", "default"))
        pages_per_batch = int(params.get("pages_per_batch") or _cp["search_pages_per_batch"])
        batch_interval_minutes = int(params.get("batch_interval_minutes") or _cp["search_batch_interval"])

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

        logger.info(f"[{self.platform}_link_search] Using context_key={ctx.context_id}")
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
            login_result = await check_login_status(agent, self.platform)
            if login_result.logged_in:
                await task.log_step(step_counter[0], f"登录态校验通过（{self.platform}）", {"platform": self.platform}, {"logged_in": True}, "completed")
            else:
                await task.log_step(step_counter[0], f"登录态校验失败（{self.platform}）", {"platform": self.platform}, {"logged_in": False, "reason": login_result.reason}, "failed")
                await task.fail(login_failed_message(self.platform))
                return None
            step_counter[0] += 1

            # 断点续跑：加载已有链接
            seen_urls: set[str] = set()
            existing_links = await ProductLink.filter(task_id=task.id)
            last_page = 0
            for link in existing_links:
                canonical = self.canonicalize_url(link.url)
                if canonical:
                    seen_urls.add(canonical)
                if link.page and link.page > last_page:
                    last_page = link.page
            total_count = len(existing_links)
            remaining = max(0, (limit or _MAX_LIMIT) - total_count) if limit else _MAX_LIMIT
            if total_count:
                logger.info(f"[{self.platform}_link_search] Resuming: {total_count} existing links, last page={last_page}, remaining={remaining}")
                task.progress = min(95, int(total_count / (limit or _MAX_LIMIT) * 100))
                task.result = {"total": total_count, "limit": limit or "不限", "keywords": keywords, "platform": self.platform}
                await task.save()
            else:
                task.progress = 5
                await task.save()

            keyword_index = 0
            keyword_count = 0

            for index, keyword in enumerate(keywords):
                if limit and total_count >= limit:
                    break
                keyword_index = index

                remaining = (limit - total_count) if limit else _MAX_LIMIT
                kw_step = step_counter[0]
                step_counter[0] += 1
                await task.log_step(kw_step, f"采集{self.platform}商品链接: {keyword}", {"keyword": keyword, "remaining": "不限" if not limit else remaining}, {}, "running")

                async def _on_page_done(page_items: list[dict], page_num: int, _keyword=keyword):
                    nonlocal total_count
                    if page_items:
                        _tags = params.get("tags") or []
                        await ProductLink.upsert_bulk([
                            ProductLink(
                                task_id=task.id,
                                platform=self.platform,
                                keyword=_keyword,
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
                                tags=_tags,
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
                        "platform": self.platform,
                    }
                    # 更新 current_offset 让前端显示进度
                    p = task.params or {}
                    p["current_offset"] = total_count
                    task.params = p
                    await task.save()
                    s = step_counter[0]
                    step_counter[0] += 1
                    await task.log_step(
                        s, f"采集第{page_num}页: {_keyword}",
                        {"keyword": _keyword, "page": page_num},
                        {"new": len(page_items), "total": total_count},
                        "completed",
                    )

                start_page = last_page if total_count and index == 0 else 1
                keyword_count = await _collect_keyword_links(
                    service=self,
                    session=session,
                    browser_context=browser_context,
                    keyword=keyword,
                    remaining=remaining,
                    max_pages=max_pages,
                    global_seen=seen_urls,
                    task_id=task.id,
                    on_page_done=_on_page_done,
                    start_page=start_page,
                    title_required=title_required,
                    title_blacklist=title_blacklist,
                    pages_per_batch=pages_per_batch,
                )

                await task.log_step(
                    kw_step,
                    f"采集{self.platform}商品链接: {keyword}",
                    {"keyword": keyword},
                    {"found": keyword_count, "total": total_count},
                    "completed",
                )
                step_counter[0] += 1

            all_links = await ProductLink.filter(task_id=task.id).order_by("created_at").limit(limit)
            link_urls = [item.url for item in all_links]
            output = json.dumps([item.to_dict() for item in all_links], ensure_ascii=False, indent=2)
            result = {
                "links": link_urls,
                "total": len(link_urls),
                "limit": limit,
                "keywords": keywords,
                "platform": self.platform,
                "keyword_index": keyword_index,
                "keyword_count": keyword_count,
                "keyword_done": keyword_count < remaining,
                "output": output,
            }

            # 分批续跑判断
            has_more = False
            if not result["keyword_done"]:
                has_more = True
            elif keyword_index < len(keywords) - 1:
                has_more = True
            elif limit and total_count < limit:
                has_more = True

            if has_more and keyword_count > 0:
                delay = batch_interval_minutes + int(random.uniform(0, _cp["batch_jitter_minutes"]))
                p = task.params or {}
                p["_batch_keyword_index"] = keyword_index if not result["keyword_done"] else keyword_index + 1
                p["pages_per_batch"] = pages_per_batch
                p["batch_interval_minutes"] = batch_interval_minutes
                task.params = p
                task.not_before_at = datetime.now() + timedelta(minutes=delay)
                task.status = TaskStatus.PENDING.value
                max_limit = 1000
                task.progress = min(95, int(total_count / (limit or max_limit) * 100)) if limit else min(95, 5 + total_count // 10)
                base_step = len(task.logs or [])
                await task.log_step(
                    base_step + 100,
                    "分批暂停，等待下次执行",
                    {"keyword_index": keyword_index, "total": total_count, "platform": self.platform},
                    {"delay_minutes": delay},
                    "completed",
                )
                await task.save()
                return {**result, "queued_next": True}

            return result

        finally:
            async def _close_browser():
                if browser:
                    cdp = await browser.new_browser_cdp_session()
                    await cdp.send("Browser.close")
                    await asyncio.sleep(0.5)
                    await browser.close()

            try:
                await asyncio.wait_for(_close_browser(), timeout=10)
            except Exception as e:
                logger.warning(f"[{self.platform}_link_search] Failed to close browser (continuing): {e!r}")

            try:
                await asyncio.wait_for(agent_bay.delete(session, sync_context=False), timeout=30)
            except Exception as e:
                logger.warning(f"[{self.platform}_link_search] Failed to delete session: {e!r}")


async def _collect_keyword_links(
    service: KeywordSearchService,
    session,
    browser_context,
    keyword: str,
    remaining: int,
    max_pages: int,
    global_seen: set[str],
    task_id=None,
    on_page_done=None,
    start_page: int = 1,
    title_required: str = "",
    title_blacklist: list[str] | None = None,
    pages_per_batch: int = 0,
) -> int:
    """采集单个关键词的商品链接。"""
    agent = session.browser.agent
    search_url = service.build_search_url(keyword)
    collected: list[dict[str, Any]] = []

    await agent.navigate(search_url)
    await asyncio.sleep(5)
    await service.after_navigate(agent)

    if start_page > 1:
        await _agent_act(agent, f"滑动到最下方点击第{start_page}页按钮。")
        await asyncio.sleep(3)

    page = await service.get_page(browser_context, search_url)
    page_index = start_page
    empty_pages = 0

    while page_index <= max_pages and len(collected) < remaining:
        if task_id:
            t = await Task.filter(id=task_id).first()
            if t and t.status == TaskStatus.CANCELLED.value:
                logger.info(f"[{service.platform}_link_search] Task cancelled, stopping")
                break

        try:
            await service.wait_page_ready(page)
            page_links = await service.scroll_and_extract(agent, page, browser_context, search_url)
        except Exception as e:
            logger.error(f"[{service.platform}_link_search] page.evaluate failed (transport closed?): {e}")
            break

        page_items = []
        for raw in page_links:
            canonical_url = service.canonicalize_url(raw.get("url", ""))
            if not canonical_url or canonical_url in global_seen:
                continue
            global_seen.add(canonical_url)
            title = raw.get("title", "")
            if title_required and title_required not in title:
                continue
            if title_blacklist and any(b in title for b in title_blacklist):
                continue

            page_items.append({
                "keyword": keyword,
                "page": page_index,
                "url": canonical_url,
                "raw_url": raw.get("url", ""),
                "title": title,
                "price": raw.get("price", ""),
                "sales": raw.get("sales", ""),
                "shop": raw.get("shop", ""),
                "image": raw.get("image", ""),
            })
            if len(collected) + len(page_items) >= remaining:
                break

        logger.info(
            f"[{service.platform}_link_search] keyword={keyword} page={page_index} "
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
            logger.info(f"[{service.platform}_link_search] keyword={keyword} 3 consecutive empty pages, stopping")
            break

        pages_in_batch = page_index - start_page + 1
        if pages_per_batch > 0 and pages_in_batch >= pages_per_batch:
            logger.info(f"[{service.platform}_link_search] keyword={keyword} batch limit reached ({pages_per_batch} pages), pausing at page {page_index}")
            break

        moved = await service.go_next_page(agent, page)
        if not moved:
            break

        page_index += 1
        await asyncio.sleep(4)
        page = await service.get_page(browser_context, search_url)

    return len(collected)


async def _agent_act(agent, instruction: str, retries: int = 3) -> bool:
    for attempt in range(retries):
        try:
            ret = await agent.act(ActOptions(action=instruction))
            return bool(getattr(ret, "success", False))
        except Exception as e:
            logger.warning(f"agent.act failed (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                await asyncio.sleep(3)
    return False


def _normalize_keywords(value) -> list[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []
